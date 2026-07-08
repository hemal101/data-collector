"""Phase 5 - Crawl the public website.

We do NOT hunt for emails here. We just fetch a small, fixed set of public
pages (home, about, contact, team, careers, privacy, terms, blog) and store the
raw HTML gzipped on disk. Parsing happens later in Phase 6.

Layout on disk (sharded so no directory holds too many entries):

    <store_root>/<xx>/<domain>/<page_type>.html.gz
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re

import httpx
from bs4 import BeautifulSoup

from pipeline.normalize import extract_domain

# page_type -> substrings that identify it in a link's href or anchor text.
PAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "about": ("about", "who-we-are", "our-story", "company"),
    "contact": ("contact", "reach-us", "get-in-touch", "connect"),
    "team": ("team", "people", "leadership", "founders", "our-team"),
    "careers": ("career", "jobs", "join-us", "hiring", "work-with-us"),
    "privacy": ("privacy",),
    "terms": ("terms", "tos", "terms-of-service", "terms-and-conditions"),
    "blog": ("blog", "news", "insights", "articles"),
}

MAX_HTML_BYTES = 3_000_000  # skip absurdly large pages


def _same_site(url: str, root_domain: str) -> bool:
    d = extract_domain(url)
    return bool(d) and (d == root_domain or d.endswith("." + root_domain))


def discover_page_urls(home_url: str, home_html: str, root_domain: str) -> dict[str, str]:
    """Map each wanted page_type to the best same-site URL found on the homepage."""
    soup = BeautifulSoup(home_html, "lxml")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        abs_url = str(httpx.URL(home_url).join(href))
        if not _same_site(abs_url, root_domain):
            continue
        hay = (href + " " + a.get_text(" ", strip=True)).lower()
        path = httpx.URL(abs_url).path.lower()
        for page_type, keys in PAGE_KEYWORDS.items():
            if page_type in found:
                continue
            if any(k in path or k in hay for k in keys):
                found[page_type] = abs_url
    return found


def _shard_dir(store_root: str, domain: str) -> str:
    h = hashlib.sha1(domain.encode()).hexdigest()[:2]
    safe = re.sub(r"[^a-z0-9.\-]", "_", domain.lower())
    return os.path.join(store_root, h, safe)


def store_html(store_root: str, domain: str, page_type: str, html_bytes: bytes) -> tuple[str, int]:
    """Gzip-write html to the sharded store; return (relative_path, byte_size)."""
    directory = _shard_dir(store_root, domain)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{page_type}.html.gz")
    with gzip.open(path, "wb") as f:
        f.write(html_bytes)
    return os.path.relpath(path, store_root), os.path.getsize(path)


def read_stored_html(store_root: str, relative_path: str) -> str:
    with gzip.open(os.path.join(store_root, relative_path), "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        resp = client.get(url)
        return resp
    except Exception:  # noqa: BLE001
        return None


def crawl_company(
    client: httpx.Client,
    company_id: int,
    home_url: str,
    domain: str,
    store_root: str,
) -> list[dict]:
    """Crawl homepage + discovered public pages. Returns crawled_pages rows."""
    pages: list[dict] = []

    home = _fetch(client, home_url)
    if home is None:
        return pages

    def _record(page_type: str, resp: httpx.Response) -> None:
        ct = resp.headers.get("content-type", "")
        row = {
            "page_type": page_type,
            "url": str(resp.url),
            "http_status": resp.status_code,
            "content_type": ct,
            "stored_path": None,
            "byte_size": None,
        }
        if "html" in ct.lower() and resp.status_code < 400:
            body = resp.content[:MAX_HTML_BYTES]
            rel, size = store_html(store_root, domain, page_type, body)
            row["stored_path"] = rel
            row["byte_size"] = size
        pages.append(row)

    _record("home", home)

    home_html = home.text if "html" in home.headers.get("content-type", "").lower() else ""
    if not home_html:
        return pages

    for page_type, url in discover_page_urls(home_url, home_html, domain).items():
        resp = _fetch(client, url)
        if resp is not None:
            _record(page_type, resp)
    return pages
