"""Phase 2 - Data normalization.

Every function here is pure (no I/O) so it can be unit-tested in isolation.
Each returns a cleaned value or ``None`` when the input is missing/garbage.

The website -> domain flow follows the spec example:

    "https://abc.com/"  ->  "https://abc.com"  ->  "abc.com"
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Values that show up in the data as "empty" placeholders.
_NULL_TOKENS = {
    "",
    "na",
    "n/a",
    "n.a.",
    "none",
    "null",
    "nil",
    "-",
    "--",
    ".",
    "not available",
    "notavailable",
    "not applicable",
    "not provided",
    "not disclosed",
    "no",
    "no website",
    "tbd",
    "xxx",
    "0",
}


def _blank(value: str | None) -> bool:
    return value is None or value.strip().lower() in _NULL_TOKENS


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


# ---------------------------------------------------------------------------
# Company names
# ---------------------------------------------------------------------------

# Canonical display form for common Indian legal suffixes.
_SUFFIX_DISPLAY = {
    "private limited": "Private Limited",
    "limited": "Limited",
    "llp": "LLP",
    "opc": "OPC",
    "pvt": "Private",
    "ltd": "Limited",
}

# Regex-based rewrites applied to the *matching key* so that
# "Pvt Ltd", "Pvt. Ltd.", "Private Ltd" all collapse to "private limited".
_KEY_SUFFIX_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bp\s*v\s*t\b\.?"), "private"),
    (re.compile(r"\bpri?vate\b"), "private"),
    (re.compile(r"\bl\s*t\s*d\b\.?"), "limited"),
    (re.compile(r"\blimited\b"), "limited"),
    (re.compile(r"\bl\.?l\.?p\.?\b"), "llp"),
    (re.compile(r"\bo\.?p\.?c\.?\b"), "opc"),
    (re.compile(r"\bincorporated\b|\binc\b"), "inc"),
    (re.compile(r"\bcorporation\b|\bcorp\b"), "corp"),
    (re.compile(r"\bcompany\b|\bco\b"), "co"),
]


def clean_company_name(value: str | None) -> str | None:
    """Return a cleaned, display-ready company name.

    - Collapses whitespace.
    - Title-cases names that arrive fully upper-cased (very common here).
    - Standardizes the casing of legal suffixes (Private Limited, LLP, ...).
    """
    if _blank(value):
        return None
    name = _collapse_ws(value)

    # Fully upper-case -> title case for readability, otherwise keep as typed
    # (preserves intentional casing like "PredictML.ai").
    if name.upper() == name and name.lower() != name:
        name = name.title()

    # Normalize suffix casing on the display form.
    def _fix_suffix(match: re.Match[str]) -> str:
        word = match.group(0)
        return _SUFFIX_DISPLAY.get(word.lower().replace(".", ""), word)

    name = re.sub(
        r"\b(private limited|private|limited|pvt\.?|ltd\.?|llp|opc)\b",
        _fix_suffix,
        name,
        flags=re.IGNORECASE,
    )
    return name or None


def company_name_key(value: str | None) -> str | None:
    """Return a normalized key used for *matching* two company names.

    "ABC Pvt Ltd", "ABC Private Limited" and "ABC Pvt. Ltd."
    all reduce to "abc private limited".
    """
    if _blank(value):
        return None
    key = value.lower()
    key = key.replace("&", " and ")
    key = re.sub(r"[.\-_/,]", " ", key)
    key = re.sub(r"[^a-z0-9 ]", " ", key)
    key = _collapse_ws(key)
    for pattern, repl in _KEY_SUFFIX_RULES:
        key = pattern.sub(repl, key)
    # "pvt" that survived on its own -> private
    key = re.sub(r"\bpvt\b", "private", key)
    key = _collapse_ws(key)
    return key or None


# ---------------------------------------------------------------------------
# Websites & domains
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


def normalize_website(value: str | None) -> str | None:
    """Return a canonical URL: ``https://<host><path>`` with no trailing slash.

    "https://abc.com/" -> "https://abc.com"
    "www.abc.com"      -> "https://abc.com"
    """
    if _blank(value):
        return None
    raw = value.strip()
    raw = re.sub(r"\s+", "", raw)  # URLs never contain spaces
    if not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    # Split scheme from the rest.
    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()
    if scheme not in {"http", "https"}:
        scheme = "https"
    host_and_path = rest.strip("/")  # also drops the trailing slash
    if not host_and_path:
        return None
    # Lower-case only the host portion, keep path casing.
    parts = host_and_path.split("/", 1)
    host = parts[0].lower()
    host = host.split("@")[-1]  # strip any userinfo
    host = host.split(":", 1)[0]  # strip port
    host = re.sub(r"^www\.", "", host)
    host = host.strip(".")  # tolerate stray leading/trailing dots
    if not _valid_host(host):
        return None
    path = ("/" + parts[1]) if len(parts) > 1 else ""
    path = path.rstrip("/")
    return f"https://{host}{path}"


def extract_domain(value: str | None) -> str | None:
    """Return the bare registrable host from a website/url: ``abc.com``."""
    if _blank(value):
        return None
    raw = re.sub(r"\s+", "", value.strip())
    raw = _SCHEME_RE.sub("", raw)
    raw = raw.split("/", 1)[0]
    raw = raw.split("@")[-1]
    raw = raw.split(":", 1)[0]
    raw = raw.lower()
    raw = re.sub(r"^www\.", "", raw)
    raw = raw.rstrip(".")
    if not _valid_host(raw):
        return None
    return raw


_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _valid_host(host: str) -> bool:
    return bool(host) and bool(_HOST_RE.match(host))


# Domains that are shared platforms / marketplaces / social / hosting, or free
# email providers. Companies frequently paste these as their "website", so they
# must NOT be used to link two companies together during deduplication.
GENERIC_DOMAINS = {
    "google.com", "google.co.in", "g.co", "sites.google.com", "play.google.com",
    "docs.google.com", "drive.google.com", "forms.gle", "goo.gl",
    "indiamart.com", "justdial.com", "tradeindia.com", "exportersindia.com",
    "facebook.com", "fb.com", "m.facebook.com", "instagram.com", "linkedin.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "whatsapp.com", "wa.me",
    "t.me", "telegram.me", "pinterest.com", "threads.net", "linktr.ee",
    "wordpress.com", "wordpress.org", "blogspot.com", "blogspot.in",
    "wixsite.com", "wix.com", "weebly.com", "godaddy.com", "squarespace.com",
    "shopify.com", "myshopify.com", "tumblr.com", "medium.com", "notion.site",
    "canva.com", "behance.net", "github.io", "github.com", "gitlab.com",
    "amazon.in", "amazon.com", "flipkart.com", "meesho.com", "etsy.com",
    "gmail.com", "yahoo.com", "yahoo.in", "hotmail.com", "outlook.com",
    "rediffmail.com", "icloud.com", "protonmail.com", "example.com",
    "apps.apple.com", "play.google.co.in", "startupindia.gov.in",
    # Company-registry / directory / lookup sites (not a company's own site).
    "zaubacorp.com", "tofler.in", "thecompanycheck.com", "falconebiz.com",
    "instafinancials.com", "quickcompany.in", "company360.in", "corpwiki.in",
    "companycheck.in", "mca.gov.in", "opencorporates.com", "dnb.co.in",
    "indiafilings.com", "vakilsearch.com", "cleartax.in", "corpbiz.io",
    "setindiabiz.com", "legalraasta.com", "indiacompanyregistration.com",
    # Hosted storefront / payment-link / linktree-style pages.
    "razorpay.com", "instamojo.com", "dukaan.io", "mydukaan.io", "bikayi.com",
    "shopdeck.com", "digital.startupindia.gov.in", "about.me", "carrd.co",
    # URL shorteners.
    "bit.ly", "tinyurl.com", "rebrand.ly", "cutt.ly", "rb.gy", "lnkd.in",
    "shorturl.at", "t.co", "ow.ly", "surl.li",
    # Obvious placeholder "websites".
    "nowebsite.com", "noweb.com", "notavailable.com", "dummy.com", "test.com",
    "website.com", "none.com", "example.org", "example.net", "abc.com",
    "xyz.com", "domain.com", "yourwebsite.com", "mywebsite.com", "na.com",
    "comingsoon.com", "underconstruction.com",
}


def is_generic_domain(domain: str | None) -> bool:
    """True if ``domain`` (or a parent of it) is a shared/placeholder host.

    Subdomain-aware: ``in.linkedin.com`` is treated the same as ``linkedin.com``.
    """
    if not domain:
        return False
    if domain in GENERIC_DOMAINS:
        return True
    return any(domain.endswith("." + g) for g in GENERIC_DOMAINS)


# Company "names" that are placeholders or too generic to link two records on.
_GENERIC_NAME_KEYS = {
    "abc", "xyz", "test", "testing", "demo", "sample", "dummy", "asdf",
    "na", "company", "startup", "my company", "new company", "private limited",
    "limited", "llp", "opc", "none", "nil", "unknown", "individual", "self",
    "proprietor", "sole proprietor", "not applicable", "aaa", "aaaa", "aaaaa",
}


def is_generic_name_key(name_key: str | None) -> bool:
    """True if a name key is a placeholder / too short to match reliably."""
    if not name_key:
        return True
    if len(name_key) < 4:
        return True
    return name_key in _GENERIC_NAME_KEYS


# ---------------------------------------------------------------------------
# States (India)
# ---------------------------------------------------------------------------

_CANONICAL_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal",
    # Union territories
    "andaman and nicobar islands", "chandigarh",
    "dadra and nagar haveli and daman and diu", "delhi",
    "jammu and kashmir", "ladakh", "lakshadweep", "puducherry",
}

_STATE_ALIASES = {
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "pondichery": "puducherry",
    "uttaranchal": "uttarakhand",
    "uttaranchal ": "uttarakhand",
    "new delhi": "delhi",
    "nct of delhi": "delhi",
    "delhi ncr": "delhi",
    "national capital territory of delhi": "delhi",
    "jammu & kashmir": "jammu and kashmir",
    "j&k": "jammu and kashmir",
    "andaman & nicobar islands": "andaman and nicobar islands",
    "andaman and nicobar": "andaman and nicobar islands",
    "dadra & nagar haveli": "dadra and nagar haveli and daman and diu",
    "daman & diu": "dadra and nagar haveli and daman and diu",
    "dadra and nagar haveli": "dadra and nagar haveli and daman and diu",
    "daman and diu": "dadra and nagar haveli and daman and diu",
    "tamilnadu": "tamil nadu",
    "chattisgarh": "chhattisgarh",
    "chhatisgarh": "chhattisgarh",
    "telengana": "telangana",
}


def normalize_state(value: str | None) -> str | None:
    if _blank(value):
        return None
    key = _collapse_ws(value.lower().replace(".", ""))
    key = key.replace(" & ", " and ")
    key = _STATE_ALIASES.get(key, key)
    if key in _CANONICAL_STATES:
        return _title_place(key)
    # Unknown value: keep a cleaned, title-cased version rather than dropping.
    return _title_place(key)


# ---------------------------------------------------------------------------
# Cities
# ---------------------------------------------------------------------------

_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "bangaluru": "bengaluru",
    "gurgaon": "gurugram",
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "trivandrum": "thiruvananthapuram",
    "cochin": "kochi",
    "mysore": "mysuru",
    "mangalore": "mangaluru",
    "baroda": "vadodara",
    "poona": "pune",
    "vizag": "visakhapatnam",
    "pondicherry": "puducherry",
    "gauhati": "guwahati",
    "benares": "varanasi",
    "banaras": "varanasi",
    "allahabad": "prayagraj",
    "gurgao": "gurugram",
    "new delhi": "new delhi",
}

_SMALL_WORDS = {"and", "of", "the"}


def _title_place(value: str) -> str:
    words = []
    for i, w in enumerate(value.split(" ")):
        if w in _SMALL_WORDS and i != 0:
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def normalize_city(value: str | None) -> str | None:
    if _blank(value):
        return None
    key = _collapse_ws(value.lower())
    key = re.sub(r"[^a-z ]", " ", key)
    key = _collapse_ws(key)
    if not key:
        return None
    key = _CITY_ALIASES.get(key, key)
    return _title_place(key)


# ---------------------------------------------------------------------------
# Industries / Sectors
# ---------------------------------------------------------------------------


def normalize_industry(value: str | None) -> str | None:
    """Trim + collapse whitespace, keep the source taxonomy's casing intact."""
    if _blank(value):
        return None
    return _collapse_ws(value) or None


# ---------------------------------------------------------------------------
# Phone numbers (India)
# ---------------------------------------------------------------------------


def normalize_phone(value: str | None) -> str | None:
    """Return a canonical ``+91XXXXXXXXXX`` Indian mobile/landline, else None.

    Handles: leading 0, country code 91/+91, stray extra digits, separators.
    """
    if _blank(value):
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    # Drop a leading 0 (STD prefix) or 91 country code, possibly repeated.
    while len(digits) > 10 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    # After stripping, a valid Indian subscriber number is 10 digits.
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    # 11 digits starting with 0 -> landline w/ STD, take last 10.
    if len(digits) > 10:
        tail = digits[-10:]
        if tail[0] in "6789":
            return "+91" + tail
    return None


# ---------------------------------------------------------------------------
# CIN, DPIIT flag, dates
# ---------------------------------------------------------------------------

# Corporate Identification Number: L/U + 5 digits + 2 letters (state) +
# 4 digit year + 3 letter company class + 6 digit registration number.
_CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")


def normalize_cin(value: str | None) -> str | None:
    """Uppercase + trim a CIN. Returns the value only if it is a valid CIN."""
    if _blank(value):
        return None
    cin = re.sub(r"\s+", "", value).upper()
    return cin if _CIN_RE.match(cin) else None


def normalize_dpiit(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "1", "yes", "y", "t"}


def normalize_status(value: str | None) -> str | None:
    if _blank(value):
        return None
    return _collapse_ws(value)


def normalize_registration_date(
    date_str: str | None, epoch_ms: str | None = None
) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` date.

    Prefers the ``dd-mm-yyyy`` ``registration date`` column, falling back to
    the ``registeredOn`` epoch-milliseconds column.
    """
    if not _blank(date_str):
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    if not _blank(epoch_ms):
        try:
            ms = int(float(epoch_ms))
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            pass
    return None
