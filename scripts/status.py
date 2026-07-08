#!/usr/bin/env python3
"""Print a Markdown progress report for the enrichment database.

Used by the GitHub Actions job summary, but handy locally too:
    python scripts/status.py [companies.db]
"""

from __future__ import annotations

import os
import sqlite3
import sys


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "companies.db"
    if not os.path.exists(db):
        print(f"_No database at {db}._")
        return 0
    c = sqlite3.connect(db)

    def q(sql: str) -> int:
        try:
            return c.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    rows = [
        ("Companies (total)", "SELECT COUNT(*) FROM companies"),
        ("Probed — Phase 4", "SELECT COUNT(*) FROM website_probes"),
        ("Alive sites", "SELECT COUNT(*) FROM website_probes WHERE alive=1"),
        ("Pages crawled — Phase 5", "SELECT COUNT(*) FROM crawled_pages"),
        ("AI-enriched — Phase 7", "SELECT COUNT(*) FROM company_ai"),
        ("Contacts — Phase 8", "SELECT COUNT(*) FROM company_contacts"),
        ("Verified emails — Phase 9", "SELECT COUNT(*) FROM company_contacts WHERE verified='verified'"),
        ("Scored — Phase 10", "SELECT COUNT(*) FROM company_scores"),
    ]

    print("### Enrichment progress\n")
    print("| Metric | Count |")
    print("|---|---:|")
    for label, sql in rows:
        print(f"| {label} | {q(sql):,} |")

    total = q("SELECT COUNT(*) FROM companies")
    probed = q("SELECT COUNT(*) FROM website_probes")
    with_domain = q("SELECT COUNT(*) FROM companies WHERE domain IS NOT NULL")
    if with_domain:
        print(f"\n_Discovery coverage: {probed:,}/{with_domain:,} domains "
              f"({100*probed/with_domain:.1f}%)._")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
