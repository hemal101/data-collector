#!/usr/bin/env python3
"""Build the master company database from rawData.csv.

Pipeline:
    Phase 1  load raw CSV
    Phase 2  normalize every field
    Phase 3  deduplicate into canonical companies
    ->       write to a SQLite master database (companies.db)

Usage:
    python3 build_database.py [--input rawData.csv] [--db companies.db]
                              [--startups-only] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import sys
import time

from pipeline import db, dedup, normalize

# rawData.csv column -> our internal field name
RAW_COLUMNS = {
    "name": "name",
    "id": "id",
    "role": "role",
    "state": "state",
    "city": "city",
    "industries": "industries",
    "sectors": "sectors",
    "stages": "stages",
    "registeredOn": "registeredOn",
    "registration date": "registration date",
    "dippCertified": "dippCertified",
    "companyStatus": "companyStatus",
    "website": "website",
    "cin": "cin",
    "contactNumber": "contactNumber",
}


def normalize_row(raw: dict) -> dict:
    """Phase 2: turn one raw CSV row into a normalized record dict."""
    website = normalize.normalize_website(raw.get("website"))
    # Domain is derived from the raw website (falls back to normalized url).
    domain = normalize.extract_domain(raw.get("website"))
    name = normalize.clean_company_name(raw.get("name"))
    return {
        "startup_india_id": (raw.get("id") or "").strip() or None,
        "name": name,
        "_name_key": normalize.company_name_key(raw.get("name")),
        "website": website,
        "domain": domain,
        "industry": normalize.normalize_industry(raw.get("industries")),
        "sector": normalize.normalize_industry(raw.get("sectors")),
        "stage": normalize.normalize_industry(raw.get("stages")),
        "city": normalize.normalize_city(raw.get("city")),
        "state": normalize.normalize_state(raw.get("state")),
        "registration_date": normalize.normalize_registration_date(
            raw.get("registration date"), raw.get("registeredOn")
        ),
        "cin": normalize.normalize_cin(raw.get("cin")),
        "dpiit_certified": normalize.normalize_dpiit(raw.get("dippCertified")),
        "status": normalize.normalize_status(raw.get("companyStatus")),
        "phone": normalize.normalize_phone(raw.get("contactNumber")),
        "role": normalize.normalize_industry(raw.get("role")),
    }


def load_and_normalize(
    path: str, *, startups_only: bool, limit: int | None
) -> list[dict]:
    records: list[dict] = []
    skipped_no_name = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader):
            if limit is not None and i >= limit:
                break
            rec = normalize_row(raw)
            if not rec["name"]:
                skipped_no_name += 1
                continue
            if startups_only and (rec["role"] or "").lower() != "startup":
                continue
            records.append(rec)
    if skipped_no_name:
        print(f"  (skipped {skipped_no_name:,} rows with no usable name)")
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description="Build master company database")
    ap.add_argument("--input", default="rawData.csv")
    ap.add_argument("--db", default="companies.db")
    ap.add_argument("--startups-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="max rows (for testing)")
    args = ap.parse_args()

    t0 = time.time()
    print(f"Phase 1/2  Reading + normalizing {args.input} ...")
    records = load_and_normalize(
        args.input, startups_only=args.startups_only, limit=args.limit
    )
    print(f"  normalized {len(records):,} records in {time.time() - t0:.1f}s")

    print("Phase 3    Deduplicating ...")
    t1 = time.time()
    clusters, stats = dedup.deduplicate(records)
    merged = [dedup.merge_cluster(records, idx) for idx in clusters]
    print(f"  {stats.total_records:,} records -> {stats.unique_companies:,} companies "
          f"({stats.duplicates_removed:,} duplicates removed) in {time.time() - t1:.1f}s")
    print(f"  links found: {stats.links_by_key}")
    print(f"  shared/aggregator domains auto-excluded: {stats.shared_domains_detected}")
    print(f"  largest merged cluster: {stats.largest_cluster} records")

    print(f"Writing     {args.db} ...")
    conn = db.connect(args.db)
    db.init_schema(conn, reset=True)
    db.insert_companies(conn, merged)

    (total,) = conn.execute("SELECT COUNT(*) FROM companies").fetchone()
    (with_domain,) = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE domain IS NOT NULL"
    ).fetchone()
    (with_cin,) = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE cin IS NOT NULL"
    ).fetchone()
    (with_phone,) = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE phone IS NOT NULL"
    ).fetchone()
    conn.close()

    print("\nDone.")
    print(f"  companies rows : {total:,}")
    print(f"  with domain    : {with_domain:,}")
    print(f"  valid CIN      : {with_cin:,}")
    print(f"  valid phone    : {with_phone:,}")
    print(f"  total time     : {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
