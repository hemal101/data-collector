"""Phase 1 - The master company database (SQLite).

`companies` is the single source of truth. A companion `company_sources` table
records every raw Startup-India id that was merged into each company, so the
deduplication is fully traceable and nothing is silently lost.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    startup_india_id  TEXT,
    name              TEXT NOT NULL,
    website           TEXT,
    domain            TEXT,
    industry          TEXT,
    sector            TEXT,
    stage             TEXT,
    city              TEXT,
    state             TEXT,
    registration_date TEXT,          -- ISO YYYY-MM-DD
    cin               TEXT,
    dpiit_certified   INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean
    status            TEXT,
    phone             TEXT,
    role              TEXT,           -- Startup / Investor / Incubator / ...
    duplicate_count   INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_sources (
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    startup_india_id  TEXT NOT NULL,
    PRIMARY KEY (company_id, startup_india_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_startup_id
    ON companies(startup_india_id) WHERE startup_india_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_companies_cin     ON companies(cin);
CREATE INDEX IF NOT EXISTS idx_companies_domain  ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_companies_name    ON companies(name);
CREATE INDEX IF NOT EXISTS idx_companies_state   ON companies(state);
CREATE INDEX IF NOT EXISTS idx_sources_startup   ON company_sources(startup_india_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(conn: sqlite3.Connection, *, reset: bool = False) -> None:
    if reset:
        conn.executescript(
            "DROP TABLE IF EXISTS company_sources; DROP TABLE IF EXISTS companies;"
        )
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_companies(conn: sqlite3.Connection, companies: list[dict]) -> None:
    """Bulk-insert merged company dicts (as produced by dedup.merge_cluster)."""
    now = _now()
    company_rows = []
    for c in companies:
        company_rows.append(
            (
                c.get("startup_india_id"),
                c.get("name"),
                c.get("website"),
                c.get("domain"),
                c.get("industry"),
                c.get("sector"),
                c.get("stage"),
                c.get("city"),
                c.get("state"),
                c.get("registration_date"),
                c.get("cin"),
                1 if c.get("dpiit_certified") else 0,
                c.get("status"),
                c.get("phone"),
                c.get("role"),
                c.get("_duplicate_count", 1),
                now,
                now,
            )
        )

    cur = conn.cursor()
    cur.execute("BEGIN;")
    prev_max = cur.execute("SELECT COALESCE(MAX(id), 0) FROM companies").fetchone()[0]
    cur.executemany(
        """
        INSERT INTO companies (
            startup_india_id, name, website, domain, industry, sector, stage,
            city, state, registration_date, cin, dpiit_certified, status,
            phone, role, duplicate_count, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        company_rows,
    )

    # executemany inserts in order and AUTOINCREMENT is monotonic, so the new
    # rows occupy ids (prev_max + 1) .. (prev_max + len) in the same order.
    first_id = prev_max + 1
    source_rows = []
    for offset, c in enumerate(companies):
        company_id = first_id + offset
        for sid in c.get("_source_ids", []):
            source_rows.append((company_id, sid))
    cur.executemany(
        "INSERT OR IGNORE INTO company_sources (company_id, startup_india_id) VALUES (?,?)",
        source_rows,
    )
    conn.commit()
