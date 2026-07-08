"""Schema + helpers for Phases 4-6 (discovery, crawl, extraction).

These tables all hang off ``companies.id`` from Phases 1-3, so the master
database stays the single source of truth. Everything is written with
``INSERT OR REPLACE`` keyed by ``company_id`` (or a natural key), which makes
every phase idempotent and safely resumable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

ENRICH_SCHEMA = """
-- Phase 4: reachability + HTTP fingerprint of the site
CREATE TABLE IF NOT EXISTS website_probes (
    company_id    INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    domain        TEXT,
    input_url     TEXT,
    resolved      INTEGER,      -- DNS A/AAAA record exists
    alive         INTEGER,      -- responded with HTTP < 400
    http_status   INTEGER,
    https_ok      INTEGER,      -- https reachable
    redirected    INTEGER,
    final_url     TEXT,
    final_scheme  TEXT,
    title         TEXT,
    favicon_url   TEXT,
    has_robots    INTEGER,
    robots_url    TEXT,
    server_header TEXT,
    error         TEXT,
    checked_at    TEXT NOT NULL
);

-- Phase 4: DNS / mail / hosting intel
CREATE TABLE IF NOT EXISTS dns_records (
    company_id       INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    domain           TEXT,
    a_records        TEXT,   -- json array
    ns_records       TEXT,   -- json array
    mx_records       TEXT,   -- json array
    has_mx           INTEGER,
    spf              TEXT,
    dmarc            TEXT,
    hosting_provider TEXT,
    checked_at       TEXT NOT NULL
);

-- Phase 5: raw HTML we crawled (bytes live gzipped on disk at stored_path)
CREATE TABLE IF NOT EXISTS crawled_pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    page_type    TEXT NOT NULL,   -- home/about/contact/team/careers/privacy/terms/blog
    url          TEXT,
    http_status  INTEGER,
    content_type TEXT,
    stored_path  TEXT,            -- gzipped html, relative to the crawl store root
    byte_size    INTEGER,
    fetched_at   TEXT NOT NULL,
    UNIQUE (company_id, page_type)
);

-- Phase 6: structured facts parsed out of the crawled HTML
CREATE TABLE IF NOT EXISTS company_enrichment (
    company_id     INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    extracted_name TEXT,
    description    TEXT,
    logo_url       TEXT,
    founded_year   INTEGER,
    employee_count TEXT,
    address        TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_emails (
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    source_page TEXT,
    PRIMARY KEY (company_id, email)
);

CREATE TABLE IF NOT EXISTS company_phones (
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    phone       TEXT NOT NULL,
    source_page TEXT,
    PRIMARY KEY (company_id, phone)
);

CREATE TABLE IF NOT EXISTS company_socials (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    platform   TEXT NOT NULL,   -- linkedin/twitter/instagram/facebook/youtube/github
    url        TEXT NOT NULL,
    PRIMARY KEY (company_id, platform)
);

CREATE TABLE IF NOT EXISTS company_tech (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    technology TEXT NOT NULL,   -- WordPress/Shopify/React/Next.js/Laravel/Cloudflare/Google Analytics/...
    category   TEXT,            -- cms/ecommerce/js-framework/backend/cdn/analytics
    evidence   TEXT,
    PRIMARY KEY (company_id, technology)
);

-- Phase 7: AI (or heuristic) enrichment
CREATE TABLE IF NOT EXISTS company_ai (
    company_id        INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    short_description TEXT,
    industry          TEXT,
    sub_industry      TEXT,
    icp               TEXT,   -- ideal customer profile
    business_type     TEXT,   -- B2B / B2C / B2B2C / Marketplace / ...
    target_market     TEXT,
    keywords          TEXT,   -- json array
    model             TEXT,   -- 'heuristic' or the LLM model name
    generated_at      TEXT NOT NULL
);

-- Phase 8: discovered / generated contact emails, each with provenance
CREATE TABLE IF NOT EXISTS company_contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    contact_type TEXT,        -- support/sales/founder/ceo/hr/marketing/info/contact/general
    name         TEXT,
    source       TEXT,        -- crawl / pattern
    confidence   REAL,        -- 0..1
    found_on     TEXT,        -- page type or url where it was seen
    verified     TEXT NOT NULL DEFAULT 'unknown',  -- unknown/verified/invalid/catch-all/disposable
    last_checked TEXT,
    UNIQUE (company_id, email)
);

-- Phase 10: lead score with a transparent breakdown
CREATE TABLE IF NOT EXISTS company_scores (
    company_id  INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    score       INTEGER NOT NULL,
    breakdown   TEXT,        -- json {factor: points}
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_alive   ON website_probes(alive);
CREATE INDEX IF NOT EXISTS idx_dns_hosting   ON dns_records(hosting_provider);
CREATE INDEX IF NOT EXISTS idx_pages_company ON crawled_pages(company_id);
CREATE INDEX IF NOT EXISTS idx_tech_tech     ON company_tech(technology);
CREATE INDEX IF NOT EXISTS idx_socials_plat  ON company_socials(platform);
CREATE INDEX IF NOT EXISTS idx_contacts_co   ON company_contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_ver  ON company_contacts(verified);
CREATE INDEX IF NOT EXISTS idx_contacts_type ON company_contacts(contact_type);
CREATE INDEX IF NOT EXISTS idx_scores_score  ON company_scores(score);
CREATE INDEX IF NOT EXISTS idx_ai_industry   ON company_ai(industry);
"""


def init_enrich_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ENRICH_SCHEMA)
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(value) -> str | None:
    return json.dumps(value) if value else None


# ---------------------------------------------------------------------------
# Phase 4 writers
# ---------------------------------------------------------------------------

def save_probe(conn: sqlite3.Connection, company_id: int, p: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO website_probes (
            company_id, domain, input_url, resolved, alive, http_status,
            https_ok, redirected, final_url, final_scheme, title, favicon_url,
            has_robots, robots_url, server_header, error, checked_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id, p.get("domain"), p.get("input_url"),
            _b(p.get("resolved")), _b(p.get("alive")), p.get("http_status"),
            _b(p.get("https_ok")), _b(p.get("redirected")), p.get("final_url"),
            p.get("final_scheme"), p.get("title"), p.get("favicon_url"),
            _b(p.get("has_robots")), p.get("robots_url"), p.get("server_header"),
            p.get("error"), now(),
        ),
    )


def save_dns(conn: sqlite3.Connection, company_id: int, d: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO dns_records (
            company_id, domain, a_records, ns_records, mx_records, has_mx,
            spf, dmarc, hosting_provider, checked_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id, d.get("domain"), _j(d.get("a_records")),
            _j(d.get("ns_records")), _j(d.get("mx_records")),
            _b(bool(d.get("mx_records"))), d.get("spf"), d.get("dmarc"),
            d.get("hosting_provider"), now(),
        ),
    )


def _b(value) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


# ---------------------------------------------------------------------------
# Phase 5 writer
# ---------------------------------------------------------------------------

def save_crawled_page(conn: sqlite3.Connection, company_id: int, page: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO crawled_pages (
            company_id, page_type, url, http_status, content_type,
            stored_path, byte_size, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            company_id, page["page_type"], page.get("url"),
            page.get("http_status"), page.get("content_type"),
            page.get("stored_path"), page.get("byte_size"), now(),
        ),
    )


# ---------------------------------------------------------------------------
# Phase 6 writers
# ---------------------------------------------------------------------------

def save_enrichment(conn: sqlite3.Connection, company_id: int, e: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO company_enrichment (
            company_id, extracted_name, description, logo_url, founded_year,
            employee_count, address, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            company_id, e.get("extracted_name"), e.get("description"),
            e.get("logo_url"), e.get("founded_year"), e.get("employee_count"),
            e.get("address"), now(),
        ),
    )
    for email in e.get("emails", []):
        conn.execute(
            "INSERT OR IGNORE INTO company_emails (company_id, email, source_page) VALUES (?,?,?)",
            (company_id, email["value"], email.get("source")),
        )
    for phone in e.get("phones", []):
        conn.execute(
            "INSERT OR IGNORE INTO company_phones (company_id, phone, source_page) VALUES (?,?,?)",
            (company_id, phone["value"], phone.get("source")),
        )
    for platform, url in e.get("socials", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO company_socials (company_id, platform, url) VALUES (?,?,?)",
            (company_id, platform, url),
        )
    for tech in e.get("technologies", []):
        conn.execute(
            "INSERT OR REPLACE INTO company_tech (company_id, technology, category, evidence) VALUES (?,?,?,?)",
            (company_id, tech["technology"], tech.get("category"), tech.get("evidence")),
        )


# ---------------------------------------------------------------------------
# Phase 7 / 8 / 10 writers
# ---------------------------------------------------------------------------

def save_ai(conn: sqlite3.Connection, company_id: int, a: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO company_ai (
            company_id, short_description, industry, sub_industry, icp,
            business_type, target_market, keywords, model, generated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id, a.get("short_description"), a.get("industry"),
            a.get("sub_industry"), a.get("icp"), a.get("business_type"),
            a.get("target_market"), _j(a.get("keywords")), a.get("model"), now(),
        ),
    )


def save_contact(conn: sqlite3.Connection, company_id: int, c: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO company_contacts (
            company_id, email, contact_type, name, source, confidence,
            found_on, verified, last_checked
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id, c["email"], c.get("contact_type"), c.get("name"),
            c.get("source"), c.get("confidence"), c.get("found_on"),
            c.get("verified", "unknown"), c.get("last_checked"),
        ),
    )


def update_contact_verification(conn: sqlite3.Connection, contact_id: int, status: str) -> None:
    conn.execute(
        "UPDATE company_contacts SET verified=?, last_checked=? WHERE id=?",
        (status, now(), contact_id),
    )


def save_score(conn: sqlite3.Connection, company_id: int, score: int, breakdown: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO company_scores (company_id, score, breakdown, computed_at) VALUES (?,?,?,?)",
        (company_id, score, _j(breakdown), now()),
    )
