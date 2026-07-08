"""Phase 8 - Contact discovery.

Turns the raw emails found while crawling (plus a few standard role-address
guesses) into a classified ``company_contacts`` list. Each contact carries its
provenance: source, confidence, found_on, verified, last_checked.

Verification status starts as 'unknown' and is filled in by Phase 9.
"""

from __future__ import annotations

import re

# local-part (before @) -> (contact_type, base_confidence)
_ROLE_MAP: list[tuple[tuple[str, ...], str, float]] = [
    (("support", "help", "care", "customercare", "helpdesk"), "support", 0.9),
    (("sales", "business", "bd", "partnerships"), "sales", 0.9),
    (("founder", "founders", "cofounder"), "founder", 0.85),
    (("ceo", "md", "director"), "ceo", 0.85),
    (("hr", "careers", "career", "jobs", "recruit", "hiring", "talent"), "hr", 0.85),
    (("marketing", "media", "press", "pr", "growth"), "marketing", 0.85),
    (("info", "information"), "info", 0.8),
    (("contact", "contactus", "connect", "reach"), "contact", 0.8),
    (("hello", "hi", "hey", "team", "admin", "office", "enquiry", "enquiries",
      "inquiry", "mail", "general", "query"), "general", 0.75),
]

# Standard addresses we generate for a domain even if not seen on the site.
_PATTERN_ADDRESSES = [
    ("info", "info"),
    ("contact", "contact"),
    ("sales", "sales"),
    ("support", "support"),
    ("hello", "general"),
]

_NAME_RE = re.compile(r"^[a-z]+[._][a-z]+$")


def classify_email(email: str) -> tuple[str, float, str | None]:
    """Return (contact_type, base_confidence, guessed_name)."""
    local = email.split("@", 1)[0].lower()
    local_clean = re.sub(r"[0-9]+$", "", local)  # drop trailing digits
    # Compare whole tokens (split on . _ -) so "careers" != "care".
    tokens = set(re.split(r"[._\-]+", local_clean))
    for needles, ctype, conf in _ROLE_MAP:
        if tokens & set(needles):
            return ctype, conf, None
    # firstname.lastname style -> a person, medium confidence.
    if _NAME_RE.match(local):
        name = " ".join(p.capitalize() for p in re.split(r"[._]", local))
        return "general", 0.7, name
    return "general", 0.6, None


def generate_pattern_emails(domain: str) -> list[dict]:
    """Standard role addresses to try for a domain (source='pattern')."""
    out = []
    for local, ctype in _PATTERN_ADDRESSES:
        out.append({
            "email": f"{local}@{domain}",
            "contact_type": ctype,
            "name": None,
            "source": "pattern",
            "confidence": 0.4,
            "found_on": None,
            "verified": "unknown",
        })
    return out


def build_contacts(
    domain: str | None,
    found_emails: list[dict],
    include_patterns: bool = True,
) -> list[dict]:
    """Merge crawled emails + generated patterns into de-duplicated contacts.

    ``found_emails`` items: {"email": ..., "source_page": ...}.
    Emails whose host differs from the company domain lose confidence (they may
    belong to an agency, founder's personal gmail, etc.).
    """
    by_email: dict[str, dict] = {}

    for row in found_emails:
        email = row["email"].strip().lower()
        if "@" not in email:
            continue
        ctype, conf, name = classify_email(email)
        host = email.split("@", 1)[1]
        if domain and host != domain and not host.endswith("." + domain):
            conf = max(0.3, conf - 0.3)  # off-domain address
        by_email[email] = {
            "email": email,
            "contact_type": ctype,
            "name": name,
            "source": "crawl",
            "confidence": round(conf, 2),
            "found_on": row.get("source_page"),
            "verified": "unknown",
        }

    if include_patterns and domain:
        for pat in generate_pattern_emails(domain):
            by_email.setdefault(pat["email"], pat)  # never override a crawled one

    return list(by_email.values())
