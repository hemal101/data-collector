"""Phase 6 - Extract structured facts from the crawled HTML.

Input is the set of stored pages for one company (page_type -> HTML string).
Output is a single dict ready for ``enrich_db.save_enrichment``.

We deliberately parse *after* crawling so this step is fast, offline, and
re-runnable without touching the network.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pipeline.normalize import normalize_phone

_CURRENT_YEAR = datetime.now().year

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?91[\-\s]?)?(?:0)?[6-9]\d{9}\b")
_YEAR_FOUNDED_RE = re.compile(
    r"(?:founded|established|est\.?|since|inception|incorporated)[^0-9]{0,20}(19\d{2}|20\d{2})",
    re.IGNORECASE,
)
_EMPLOYEES_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*\+?\s*(?:employees|team members|people|professionals)",
    re.IGNORECASE,
)
_TEAM_OF_RE = re.compile(r"team of\s+(\d{1,3}(?:,\d{3})*|\d+)\+?", re.IGNORECASE)
_PIN_RE = re.compile(r"\b\d{6}\b")

_EMAIL_JUNK = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "@2x", "@3x")
_EMAIL_JUNK_DOMAINS = (
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "sentry.io", "wix.com", "wixpress.com", "sentry-next.wixpress.com",
    "godaddy.com", "schema.org", "w3.org", "png", "jpg",
)

_SOCIAL_HOSTS = {
    "linkedin": ("linkedin.com",),
    "twitter": ("twitter.com", "x.com"),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "github": ("github.com",),
}
_SOCIAL_SKIP = ("/share", "/sharer", "/intent", "sharer.php", "/tr?", "plugins/")
# Generic, non-profile paths that show up but don't identify a company account.
_SOCIAL_NON_PROFILE = {
    "", "/", "/watch", "/home", "/login", "/signup", "/feed", "/results",
    "/hashtag", "/explore", "/about", "/help",
}


# ---------------------------------------------------------------------------
# Technology fingerprints
# ---------------------------------------------------------------------------
# Each entry: (technology, category, list-of-substrings that count as evidence)
_TECH_SIGNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("WordPress", "cms", ("/wp-content/", "/wp-includes/", "wp-json", "wp-emoji")),
    ("Shopify", "ecommerce", ("cdn.shopify.com", "/cdn/shop/", "shopify.theme", "x-shopify", "myshopify.com")),
    ("Wix", "cms", ("static.wixstatic.com", "wix.com", "_wixcssinjover", "wixsite.com")),
    ("Squarespace", "cms", ("static1.squarespace.com", "squarespace.com", "static.squarespace")),
    ("Webflow", "cms", ("assets.website-files.com", "assets-global.website-files.com", "webflow.io", "wf-")),
    ("Drupal", "cms", ("/sites/default/files", "drupal-settings-json", "drupal.js")),
    ("Next.js", "js-framework", ("__next_data__", "/_next/static", "/_next/", "next/dist")),
    ("React", "js-framework", ("data-reactroot", "react-dom", "_reactlistening", "__reactcontainer", "react.production.min")),
    ("Vue.js", "js-framework", ("data-v-", "__vue__", "vue.runtime", "vue.min.js")),
    ("Angular", "js-framework", ("ng-version", "ng-app", "angular.min.js", "_ngcontent")),
    ("Nuxt.js", "js-framework", ("__nuxt__", "/_nuxt/")),
    ("Gatsby", "js-framework", ("___gatsby", "gatsby-", "/page-data/")),
    ("Laravel", "backend", ("laravel_session", "xsrf-token", "/vendor/laravel", "laravel")),
    ("Django", "backend", ("csrfmiddlewaretoken", "__admin_media_prefix__")),
    ("Ruby on Rails", "backend", ("csrf-param", "rails-", "data-turbo")),
    ("Cloudflare", "cdn", ("cdn-cgi/", "__cf_bm", "cf-ray", "email-protection", "challenge-platform", "cloudflareinsights")),
    ("Google Analytics", "analytics", ("googletagmanager.com/gtag/js", "google-analytics.com/analytics.js", "googleanalyticsobject", "ga('create", "gtag(")),
    ("Google Tag Manager", "analytics", ("googletagmanager.com/gtm.js", "gtm-")),
    ("Facebook Pixel", "analytics", ("connect.facebook.net", "fbq(", "fbevents.js")),
    ("HubSpot", "marketing", ("js.hs-scripts.com", "hs-analytics", "hubspot")),
    ("Hotjar", "analytics", ("static.hotjar.com", "hotjar")),
    ("Bootstrap", "ui", ("bootstrap.min.css", "bootstrap.bundle", "class=\"container")),
    ("jQuery", "js-library", ("jquery.min.js", "jquery-", "/jquery.js")),
    ("Elementor", "cms", ("elementor-", "/elementor/")),
]

# GA measurement / property id patterns (strong evidence for GA).
_GA_ID_RE = re.compile(r"\b(?:UA-\d{4,}-\d+|G-[A-Z0-9]{6,}|GTM-[A-Z0-9]{4,})\b")


def detect_technologies(html: str, headers: dict | None = None) -> list[dict]:
    """Return a list of {technology, category, evidence} detected in the HTML."""
    hay = html.lower()
    hits: dict[str, dict] = {}
    for tech, category, needles in _TECH_SIGNS:
        for n in needles:
            if n in hay:
                hits[tech] = {"technology": tech, "category": category, "evidence": n}
                break

    ga_id = _GA_ID_RE.search(html)
    if ga_id:
        gid = ga_id.group(0)
        if gid.startswith("GTM-"):
            hits.setdefault("Google Tag Manager", {"technology": "Google Tag Manager", "category": "analytics"})["evidence"] = gid
        else:
            hits.setdefault("Google Analytics", {"technology": "Google Analytics", "category": "analytics"})["evidence"] = gid

    if headers:
        server = (headers.get("server") or "").lower()
        powered = (headers.get("x-powered-by") or "").lower()
        if "cloudflare" in server:
            hits["Cloudflare"] = {"technology": "Cloudflare", "category": "cdn", "evidence": "server: cloudflare"}
        if "php" in powered:
            hits.setdefault("PHP", {"technology": "PHP", "category": "backend", "evidence": f"x-powered-by: {powered}"})
        if "express" in powered:
            hits.setdefault("Express", {"technology": "Express", "category": "backend", "evidence": f"x-powered-by: {powered}"})
    return list(hits.values())


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, *, name=None, prop=None) -> str | None:
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _json_ld(soup: BeautifulSoup) -> list[dict]:
    out = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                out.extend(d for d in data["@graph"] if isinstance(d, dict))
            else:
                out.append(data)
    return out


def _clean_emails(raw: set[str]) -> list[str]:
    out = []
    for e in raw:
        el = e.lower().strip(".")
        if any(j in el for j in _EMAIL_JUNK):
            continue
        if any(el.endswith("@" + d) or el.endswith("." + d) for d in _EMAIL_JUNK_DOMAINS):
            continue
        if len(el) > 100 or el.count("@") != 1:
            continue
        out.append(el)
    return sorted(set(out))


def _social_platform(url: str) -> str | None:
    low = url.lower()
    if any(s in low for s in _SOCIAL_SKIP):
        return None
    for platform, hosts in _SOCIAL_HOSTS.items():
        if not any(h in low for h in hosts):
            continue
        # Require an actual profile path (not a bare root or generic page).
        path = urlsplit(url).path.rstrip("/")
        if path.lower() in _SOCIAL_NON_PROFILE:
            return None
        # youtu.be short links carry the id in the path, which is fine.
        return platform
    return None


def extract_from_pages(pages: dict[str, str], headers: dict | None = None) -> dict:
    """Parse a company's crawled pages into a single enrichment dict."""
    result: dict = {
        "extracted_name": None, "description": None, "logo_url": None,
        "founded_year": None, "employee_count": None, "address": None,
        "emails": [], "phones": [], "socials": {}, "technologies": [],
    }
    if not pages:
        return result

    emails: dict[str, str] = {}
    phones: dict[str, str] = {}
    socials: dict[str, str] = {}
    techs: dict[str, dict] = {}

    # Prefer the homepage for identity fields; fall back to any page.
    ordered = sorted(pages.items(), key=lambda kv: 0 if kv[0] == "home" else 1)

    for page_type, html in ordered:
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        # --- identity (first non-empty wins, homepage first) ---
        if not result["description"]:
            result["description"] = _meta(soup, prop="og:description") or _meta(soup, name="description")
        if not result["extracted_name"]:
            result["extracted_name"] = _meta(soup, prop="og:site_name")
        if not result["logo_url"]:
            og_img = _meta(soup, prop="og:image")
            icon = soup.find("link", rel=lambda v: v and "apple-touch-icon" in v)
            result["logo_url"] = og_img or (icon["href"] if icon and icon.get("href") else None)

        # --- structured data (schema.org Organization) ---
        for node in _json_ld(soup):
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if any(t in ("Organization", "Corporation", "LocalBusiness") for t in types):
                if not result["extracted_name"] and node.get("name"):
                    result["extracted_name"] = node["name"]
                if not result["founded_year"] and node.get("foundingDate"):
                    y = re.search(r"(19\d{2}|20\d{2})", str(node["foundingDate"]))
                    if y:
                        result["founded_year"] = int(y.group(1))
                addr = node.get("address")
                if not result["address"] and isinstance(addr, dict):
                    parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
                    result["address"] = ", ".join(str(p) for p in parts if p) or None

        # --- founded year / employees from body text ---
        if not result["founded_year"]:
            m = _YEAR_FOUNDED_RE.search(text)
            if m and 1900 <= int(m.group(1)) <= _CURRENT_YEAR:
                result["founded_year"] = int(m.group(1))
        if not result["employee_count"]:
            m = _EMPLOYEES_RE.search(text) or _TEAM_OF_RE.search(text)
            if m:
                result["employee_count"] = m.group(0).strip()
        if not result["address"] and page_type == "contact":
            result["address"] = _guess_address(text)

        # --- contacts + socials + tech (aggregate across pages) ---
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                if _EMAIL_RE.fullmatch(addr):
                    emails.setdefault(addr.lower(), page_type)
            elif href.lower().startswith("tel:"):
                ph = normalize_phone(href[4:])
                if ph:
                    phones.setdefault(ph, page_type)
            else:
                plat = _social_platform(href)
                if plat and plat not in socials:
                    socials[plat] = href.split("?")[0]

        for e in _EMAIL_RE.findall(html):
            emails.setdefault(e.lower(), page_type)
        for raw in _PHONE_RE.findall(text):
            ph = normalize_phone(raw)
            if ph:
                phones.setdefault(ph, page_type)

        for tech in detect_technologies(html, headers if page_type == "home" else None):
            techs.setdefault(tech["technology"], tech)

    result["emails"] = [{"value": v, "source": s} for v, s in _finalize_emails(emails)]
    result["phones"] = [{"value": v, "source": s} for v, s in phones.items()]
    result["socials"] = socials
    result["technologies"] = list(techs.values())
    if result["description"]:
        result["description"] = re.sub(r"\s+", " ", result["description"]).strip()[:1000]
    return result


def _finalize_emails(emails: dict[str, str]) -> list[tuple[str, str]]:
    clean = _clean_emails(set(emails.keys()))
    return [(e, emails.get(e, "")) for e in clean]


def _guess_address(text: str) -> str | None:
    m = _PIN_RE.search(text)
    if not m:
        return None
    start = max(0, m.start() - 80)
    snippet = text[start:m.end()].strip()
    return re.sub(r"\s+", " ", snippet)[-160:] or None
