"""Phase 4 - Website discovery.

For a company that has a website we record, in order:
    resolved? -> redirect? -> https? -> alive? -> favicon -> title -> robots.txt
plus DNS: A / NS / MX, SPF, DMARC, and a best-effort hosting provider.

Nothing here writes to the DB; callers persist the returned dicts.
"""

from __future__ import annotations

import re
import socket

import dns.resolver
import dns.reversename
import httpx

USER_AGENT = (
    "Mozilla/5.0 (compatible; MarketingDataBot/1.0; +company-enrichment)"
)

_DEFAULT_TIMEOUT = httpx.Timeout(12.0, connect=8.0)


def make_client() -> httpx.Client:
    """A redirect-following client tolerant of the messy TLS on small sites."""
    return httpx.Client(
        follow_redirects=True,
        timeout=_DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        verify=False,  # many startup sites have broken/expired certs
        max_redirects=6,
    )


def _resolver() -> dns.resolver.Resolver:
    r = dns.resolver.Resolver()
    r.timeout = 4.0
    r.lifetime = 6.0
    return r


def _query(res: dns.resolver.Resolver, name: str, rtype: str) -> list[str]:
    try:
        answers = res.resolve(name, rtype)
        return [r.to_text() for r in answers]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
            dns.resolver.NoNameservers, dns.exception.Timeout,
            dns.resolver.LifetimeTimeout):
        return []
    except Exception:  # noqa: BLE001 - DNS libs raise a zoo of errors
        return []


def lookup_dns(domain: str) -> dict:
    """Resolve A/NS/MX + SPF/DMARC TXT records and infer a hosting provider."""
    res = _resolver()
    a = _query(res, domain, "A")
    ns = _query(res, domain, "NS")
    mx_raw = _query(res, domain, "MX")
    # MX records look like "10 mail.example.com." -> keep host, sort by priority.
    mx = []
    for rec in sorted(mx_raw, key=lambda s: int(s.split()[0]) if s.split()[0].isdigit() else 999):
        parts = rec.split()
        mx.append(parts[1].rstrip(".") if len(parts) > 1 else rec)

    txt = _query(res, domain, "TXT")
    spf = next((t.strip('"') for t in txt if "v=spf1" in t.lower()), None)

    dmarc_txt = _query(res, f"_dmarc.{domain}", "TXT")
    dmarc = next((t.strip('"') for t in dmarc_txt if "v=dmarc1" in t.lower()), None)

    reverse = _reverse_dns(a[0]) if a else None
    hosting = detect_hosting_provider(a, ns, reverse, None)

    return {
        "domain": domain,
        "a_records": a,
        "ns_records": [n.rstrip(".") for n in ns],
        "mx_records": mx,
        "spf": spf,
        "dmarc": dmarc,
        "hosting_provider": hosting,
        "_reverse_dns": reverse,
    }


def _reverse_dns(ip: str) -> str | None:
    try:
        socket.setdefaulttimeout(4)
        return socket.gethostbyaddr(ip)[0]
    except Exception:  # noqa: BLE001
        return None


# (needle, provider) checked against ns / reverse-dns / server-header strings.
_HOSTING_SIGNS = [
    ("cloudflare", "Cloudflare"),
    ("amazonaws", "Amazon AWS"),
    ("aws", "Amazon AWS"),
    ("1e100.net", "Google"),
    ("googleusercontent", "Google Cloud"),
    ("google", "Google"),
    ("azure", "Microsoft Azure"),
    ("windows.net", "Microsoft Azure"),
    ("netlify", "Netlify"),
    ("vercel", "Vercel"),
    ("github", "GitHub Pages"),
    ("shopify", "Shopify"),
    ("wixdns", "Wix"),
    ("wix.com", "Wix"),
    ("squarespace", "Squarespace"),
    ("hostinger", "Hostinger"),
    ("secureserver.net", "GoDaddy"),
    ("domaincontrol.com", "GoDaddy"),
    ("bluehost", "Bluehost"),
    ("hostgator", "HostGator"),
    ("websitewelcome", "HostGator"),
    ("bigrock", "BigRock"),
    ("hostwinds", "Hostwinds"),
    ("digitalocean", "DigitalOcean"),
    ("ns.hostgpu", "Hosting.com"),
    ("namecheap", "Namecheap"),
    ("registrar-servers.com", "Namecheap"),
    ("fastly", "Fastly"),
    ("akamai", "Akamai"),
]

# GitHub Pages publishes on these fixed IPs.
_GITHUB_PAGES_IPS = {"185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"}


def detect_hosting_provider(
    a_records: list[str],
    ns_records: list[str],
    reverse_dns: str | None,
    server_header: str | None,
) -> str | None:
    if any(ip in _GITHUB_PAGES_IPS for ip in a_records):
        return "GitHub Pages"
    haystack = " ".join(
        [*(ns_records or []), reverse_dns or "", server_header or ""]
    ).lower()
    for needle, provider in _HOSTING_SIGNS:
        if needle in haystack:
            return provider
    if server_header:
        # Fall back to the raw server banner (e.g. "Apache", "nginx", "LiteSpeed").
        return server_header.split("/")[0].split("(")[0].strip() or None
    return None


# ---------------------------------------------------------------------------
# HTTP probe
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ICON_RE = re.compile(
    r"<link[^>]+rel=[\"']?[^\"'>]*icon[^\"'>]*[\"']?[^>]*>", re.IGNORECASE
)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _extract_title_favicon(html: str, base_url: str) -> tuple[str | None, str | None]:
    title = None
    m = _TITLE_RE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:300] or None
    favicon = None
    icon_tag = _ICON_RE.search(html)
    if icon_tag:
        href = _HREF_RE.search(icon_tag.group(0))
        if href:
            favicon = httpx.URL(base_url).join(href.group(1)).__str__()
    if not favicon:
        favicon = str(httpx.URL(base_url).copy_with(path="/favicon.ico", query=None, fragment=None))
    return title, favicon


def probe_website(client: httpx.Client, domain: str, input_url: str | None) -> dict:
    """Probe reachability + fingerprint. Tries https first, then http."""
    result: dict = {
        "domain": domain,
        "input_url": input_url,
        "resolved": None,
        "alive": False,
        "http_status": None,
        "https_ok": False,
        "redirected": False,
        "final_url": None,
        "final_scheme": None,
        "title": None,
        "favicon_url": None,
        "has_robots": None,
        "robots_url": None,
        "server_header": None,
        "error": None,
    }

    # DNS resolution check (independent of HTTP).
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, None)
        result["resolved"] = True
    except Exception:  # noqa: BLE001
        result["resolved"] = False

    candidates = [f"https://{domain}", f"http://{domain}"]
    resp = None
    last_err = None
    for url in candidates:
        try:
            resp = client.get(url)
            if url.startswith("https://"):
                result["https_ok"] = True
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"[:200]
            continue

    if resp is None:
        result["error"] = last_err or "unreachable"
        return result

    result["http_status"] = resp.status_code
    result["alive"] = resp.status_code < 400
    result["final_url"] = str(resp.url)
    result["final_scheme"] = resp.url.scheme
    result["redirected"] = str(resp.url).rstrip("/") != (input_url or candidates[0]).rstrip("/")
    result["server_header"] = resp.headers.get("server")

    ct = resp.headers.get("content-type", "")
    if "html" in ct.lower() and resp.status_code < 400:
        try:
            html = resp.text
            result["title"], result["favicon_url"] = _extract_title_favicon(html, str(resp.url))
        except Exception:  # noqa: BLE001
            pass

    # robots.txt on the final origin.
    try:
        robots_url = str(resp.url.copy_with(path="/robots.txt", query=None, fragment=None))
        r = client.get(robots_url)
        result["robots_url"] = robots_url
        result["has_robots"] = r.status_code == 200 and bool(r.text.strip())
    except Exception:  # noqa: BLE001
        result["has_robots"] = False

    return result
