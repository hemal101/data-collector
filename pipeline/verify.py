"""Phase 9 - Email verification.

Classifies each email as one of:
    verified   - mail server accepts it (and the domain is not catch-all)
    invalid    - bad syntax, no mail server, or the server rejects it
    catch-all  - the domain accepts *any* address, so we can't confirm this one
    disposable - throwaway/temerary-mail domain
    unknown    - temporary failure / greylisting / timeout

Rule from the spec: never import 'invalid'. In practice you also treat
'disposable' as unusable and 'catch-all'/'unknown' as low-confidence.

Uses SMTP RCPT probing with a null sender (MAIL FROM:<>), which is the polite,
standard way to test deliverability without sending anything.
"""

from __future__ import annotations

import random
import re
import smtplib
import string
import threading

import dns.resolver

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# A compact list of common disposable / throwaway email domains.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "guerrillamail.info",
    "tempmail.com", "temp-mail.org", "trashmail.com", "yopmail.com", "getnada.com",
    "throwawaymail.com", "fakeinbox.com", "sharklasers.com", "maildrop.cc",
    "dispostable.com", "mailnesia.com", "mvrht.net", "spamgourmet.com",
    "tempinbox.com", "emailondeck.com", "mohmal.com", "moakt.com", "burnermail.io",
    "tempmailo.com", "1secmail.com", "tmpmail.org", "discard.email",
}

STATUS_INVALID = "invalid"
STATUS_VERIFIED = "verified"
STATUS_CATCH_ALL = "catch-all"
STATUS_DISPOSABLE = "disposable"
STATUS_UNKNOWN = "unknown"


def is_valid_syntax(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254


def is_disposable(domain: str) -> bool:
    return domain.lower() in DISPOSABLE_DOMAINS


def classify_rcpt_code(code: int | None) -> str:
    """Map an SMTP RCPT reply code to a coarse outcome."""
    if code is None:
        return STATUS_UNKNOWN
    if code in (250, 251):
        return "accepted"
    if 400 <= code < 500:
        return STATUS_UNKNOWN          # greylist / temp failure
    if code >= 500:
        return "rejected"
    return STATUS_UNKNOWN


def _random_local(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class Verifier:
    """Thread-safe email verifier with per-domain MX & catch-all caching."""

    def __init__(self, sender: str = "", helo: str = "verifier.local", timeout: float = 12.0) -> None:
        self.sender = sender
        self.helo = helo
        self.timeout = timeout
        self._mx_cache: dict[str, list[str]] = {}
        self._catchall_cache: dict[str, bool | None] = {}
        self._lock = threading.Lock()

    # -- DNS ----------------------------------------------------------------
    def _mx_hosts(self, domain: str) -> list[str]:
        with self._lock:
            if domain in self._mx_cache:
                return self._mx_cache[domain]
        hosts: list[str] = []
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=6.0)
            hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
        except Exception:  # noqa: BLE001
            # Fall back to an A record - a host with an A can still accept mail.
            try:
                dns.resolver.resolve(domain, "A", lifetime=5.0)
                hosts = [domain]
            except Exception:  # noqa: BLE001
                hosts = []
        with self._lock:
            self._mx_cache[domain] = hosts
        return hosts

    # -- SMTP ---------------------------------------------------------------
    def _rcpt_codes(self, mx_host: str, recipients: list[str]) -> list[int | None]:
        """Open one SMTP session and probe several recipients."""
        codes: list[int | None] = [None] * len(recipients)
        try:
            server = smtplib.SMTP(mx_host, 25, timeout=self.timeout)
        except Exception:  # noqa: BLE001
            return codes
        try:
            server.ehlo_or_helo_if_needed()
            server.mail(self.sender)
            for i, rcpt in enumerate(recipients):
                try:
                    code, _ = server.rcpt(rcpt)
                    codes[i] = code
                except Exception:  # noqa: BLE001
                    codes[i] = None
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return codes

    def _is_catch_all(self, domain: str, mx_host: str) -> bool | None:
        with self._lock:
            if domain in self._catchall_cache:
                return self._catchall_cache[domain]
        probe = f"{_random_local()}@{domain}"
        code = self._rcpt_codes(mx_host, [probe])[0]
        result = classify_rcpt_code(code) == "accepted"
        with self._lock:
            self._catchall_cache[domain] = result
        return result

    # -- public -------------------------------------------------------------
    def verify(self, email: str) -> str:
        email = email.strip().lower()
        if not is_valid_syntax(email):
            return STATUS_INVALID
        domain = email.split("@", 1)[1]
        if is_disposable(domain):
            return STATUS_DISPOSABLE
        mx = self._mx_hosts(domain)
        if not mx:
            return STATUS_INVALID  # nowhere to deliver mail

        mx_host = mx[0]
        code = self._rcpt_codes(mx_host, [email])[0]
        outcome = classify_rcpt_code(code)
        if outcome == "rejected":
            return STATUS_INVALID
        if outcome == STATUS_UNKNOWN:
            return STATUS_UNKNOWN
        # Accepted -> distinguish a real mailbox from a catch-all domain.
        if self._is_catch_all(domain, mx_host):
            return STATUS_CATCH_ALL
        return STATUS_VERIFIED
