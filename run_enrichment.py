#!/usr/bin/env python3
"""Phases 4-6 orchestrator: website discovery, crawling, extraction.

Runs against the master ``companies.db`` produced by ``build_database.py``.
Every subcommand is incremental & resumable - it only processes companies that
haven't been done yet, so you can stop and restart freely.

    # Phase 4: probe reachability + DNS/MX/SPF/DMARC/hosting
    python run_enrichment.py discover --limit 500 --random

    # Phase 5: crawl public pages of the reachable sites -> gzipped HTML on disk
    python run_enrichment.py crawl --limit 500

    # Phase 6: parse the stored HTML into structured facts
    python run_enrichment.py extract --limit 500

Use the venv interpreter:  .venv/bin/python run_enrichment.py ...
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pipeline import (
    ai_enrich, contacts, crawl, discovery, enrich_db, extract, llm, score, verify,
)

DEFAULT_DB = "companies.db"
DEFAULT_STORE = "crawl_store"


def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    enrich_db.init_enrich_schema(conn)
    return conn


def _order(random: bool) -> str:
    return "ORDER BY RANDOM()" if random else "ORDER BY c.id"


# ---------------------------------------------------------------------------
# Phase 4
# ---------------------------------------------------------------------------

def cmd_discover(args) -> None:
    conn = _connect(args.db)
    rows = conn.execute(
        f"""
        SELECT c.id, c.domain, c.website
        FROM companies c
        WHERE c.domain IS NOT NULL
          AND c.id NOT IN (SELECT company_id FROM website_probes)
        {_order(args.random)}
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"[discover] {len(rows)} companies to probe (workers={args.workers})")

    tls = threading.local()

    def _client() -> "discovery.httpx.Client":
        if not hasattr(tls, "client"):
            tls.client = discovery.make_client()
        return tls.client

    def work(row):
        cid, domain, website = row
        dns = discovery.lookup_dns(domain)
        probe = discovery.probe_website(_client(), domain, website)
        # Refine hosting using the HTTP Server header too.
        dns["hosting_provider"] = discovery.detect_hosting_provider(
            dns.get("a_records", []), dns.get("ns_records", []),
            dns.get("_reverse_dns"), probe.get("server_header"),
        )
        return cid, probe, dns

    done = alive = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for cid, probe, dns in ex.map(work, rows):
            enrich_db.save_probe(conn, cid, probe)
            enrich_db.save_dns(conn, cid, dns)
            done += 1
            alive += 1 if probe.get("alive") else 0
            if done % 50 == 0:
                conn.commit()
                print(f"  {done}/{len(rows)}  alive={alive}  ({done/(time.time()-t0):.1f}/s)")
    conn.commit()
    print(f"[discover] done: {done} probed, {alive} alive, {time.time()-t0:.1f}s")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 5
# ---------------------------------------------------------------------------

def cmd_crawl(args) -> None:
    conn = _connect(args.db)
    rows = conn.execute(
        f"""
        SELECT c.id, c.domain, p.final_url
        FROM website_probes p
        JOIN companies c ON c.id = p.company_id
        WHERE p.alive = 1 AND p.final_url IS NOT NULL
          AND p.company_id NOT IN (SELECT DISTINCT company_id FROM crawled_pages)
        {_order(args.random)}
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"[crawl] {len(rows)} sites to crawl -> {args.store}/ (workers={args.workers})")

    tls = threading.local()

    def _client():
        if not hasattr(tls, "client"):
            tls.client = discovery.make_client()
        return tls.client

    def work(row):
        cid, domain, final_url = row
        pages = crawl.crawl_company(_client(), cid, final_url, domain, args.store)
        return cid, pages

    done = total_pages = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for cid, pages in ex.map(work, rows):
            for page in pages:
                enrich_db.save_crawled_page(conn, cid, page)
            total_pages += len(pages)
            done += 1
            if done % 25 == 0:
                conn.commit()
                print(f"  {done}/{len(rows)}  pages={total_pages}  ({done/(time.time()-t0):.1f} sites/s)")
    conn.commit()
    print(f"[crawl] done: {done} sites, {total_pages} pages stored, {time.time()-t0:.1f}s")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 6
# ---------------------------------------------------------------------------

def cmd_extract(args) -> None:
    conn = _connect(args.db)
    company_ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT company_id FROM crawled_pages
            WHERE company_id NOT IN (SELECT company_id FROM company_enrichment)
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    ]
    print(f"[extract] {len(company_ids)} companies to parse")

    done = 0
    t0 = time.time()
    for cid in company_ids:
        page_rows = conn.execute(
            "SELECT page_type, stored_path FROM crawled_pages WHERE company_id=? AND stored_path IS NOT NULL",
            (cid,),
        ).fetchall()
        headers = None
        server = conn.execute(
            "SELECT server_header FROM website_probes WHERE company_id=?", (cid,)
        ).fetchone()
        if server and server[0]:
            headers = {"server": server[0]}

        pages = {}
        for page_type, path in page_rows:
            try:
                pages[page_type] = crawl.read_stored_html(args.store, path)
            except OSError:
                continue
        enrichment = extract.extract_from_pages(pages, headers)
        enrich_db.save_enrichment(conn, cid, enrichment)
        done += 1
        if done % 100 == 0:
            conn.commit()
            print(f"  {done}/{len(company_ids)}")
    conn.commit()
    print(f"[extract] done: {done} companies, {time.time()-t0:.1f}s")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 7 - AI enrichment
# ---------------------------------------------------------------------------

def cmd_ai(args) -> None:
    conn = _connect(args.db)
    model = llm.get_llm()
    print(f"[ai] provider: {model.model if model else 'heuristic (no API key set)'}")
    rows = conn.execute(
        f"""
        SELECT c.id, c.name, c.industry, c.sector, c.stage, c.city, c.state,
               c.website, e.description
        FROM companies c
        JOIN company_enrichment e ON e.company_id = c.id
        WHERE c.id NOT IN (SELECT company_id FROM company_ai)
        {_order(args.random)}
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"[ai] {len(rows)} companies to enrich")

    done = 0
    t0 = time.time()
    for r in rows:
        cid = r[0]
        techs = [t[0] for t in conn.execute(
            "SELECT technology FROM company_tech WHERE company_id=?", (cid,)
        ).fetchall()]
        company = {
            "name": r[1], "industry": r[2], "sector": r[3], "stage": r[4],
            "city": r[5], "state": r[6], "website": r[7], "description": r[8],
            "technologies": techs,
        }
        enrich_db.save_ai(conn, cid, ai_enrich.enrich(company, model))
        done += 1
        if done % 100 == 0:
            conn.commit()
            print(f"  {done}/{len(rows)}")
    conn.commit()
    print(f"[ai] done: {done} enriched, {time.time()-t0:.1f}s")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 8 - Contact discovery
# ---------------------------------------------------------------------------

def cmd_contacts(args) -> None:
    conn = _connect(args.db)
    rows = conn.execute(
        f"""
        SELECT c.id, c.domain
        FROM companies c
        JOIN company_enrichment e ON e.company_id = c.id
        WHERE c.id NOT IN (SELECT DISTINCT company_id FROM company_contacts)
        {_order(args.random)}
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"[contacts] {len(rows)} companies")

    total = 0
    for cid, domain in rows:
        found = [
            {"email": e, "source_page": s}
            for e, s in conn.execute(
                "SELECT email, source_page FROM company_emails WHERE company_id=?", (cid,)
            ).fetchall()
        ]
        for contact in contacts.build_contacts(domain, found, include_patterns=not args.no_patterns):
            enrich_db.save_contact(conn, cid, contact)
            total += 1
    conn.commit()
    print(f"[contacts] done: {total} contacts across {len(rows)} companies")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 9 - Email verification
# ---------------------------------------------------------------------------

def cmd_verify(args) -> None:
    conn = _connect(args.db)
    rows = conn.execute(
        "SELECT id, email FROM company_contacts WHERE verified='unknown' LIMIT ?",
        (args.limit,),
    ).fetchall()
    print(f"[verify] {len(rows)} emails to verify (workers={args.workers}, sender='{args.sender}')")

    verifier = verify.Verifier(sender=args.sender, helo=args.helo)
    from collections import Counter
    tally: "Counter[str]" = Counter()
    done = 0
    t0 = time.time()

    def work(row):
        cid_email = row
        return row[0], verifier.verify(row[1])

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for contact_id, status in ex.map(work, rows):
            enrich_db.update_contact_verification(conn, contact_id, status)
            tally[status] += 1
            done += 1
            if done % 50 == 0:
                conn.commit()
                print(f"  {done}/{len(rows)}  {dict(tally)}")
    conn.commit()
    print(f"[verify] done: {dict(tally)} in {time.time()-t0:.1f}s")
    conn.close()


# ---------------------------------------------------------------------------
# Phase 10 - Scoring
# ---------------------------------------------------------------------------

_FACTS_SQL = """
SELECT
  (c.domain IS NOT NULL)                                                        AS has_website,
  COALESCE(p.alive, 0)                                                          AS active_website,
  COALESCE(p.https_ok, 0)                                                       AS https,
  EXISTS(SELECT 1 FROM crawled_pages cp WHERE cp.company_id=c.id AND cp.page_type='contact') AS contact_page,
  EXISTS(SELECT 1 FROM company_contacts ct WHERE ct.company_id=c.id AND ct.verified NOT IN ('invalid','disposable')) AS has_email,
  EXISTS(SELECT 1 FROM company_contacts ct WHERE ct.company_id=c.id AND ct.verified='verified') AS has_verified_email,
  EXISTS(SELECT 1 FROM company_socials s WHERE s.company_id=c.id AND s.platform='linkedin') AS has_linkedin,
  (COALESCE(e.description, a.short_description) IS NOT NULL)                     AS has_description,
  EXISTS(SELECT 1 FROM company_socials s WHERE s.company_id=c.id)               AS social_presence
FROM companies c
LEFT JOIN website_probes p    ON p.company_id = c.id
LEFT JOIN company_enrichment e ON e.company_id = c.id
LEFT JOIN company_ai a         ON a.company_id = c.id
WHERE c.id = ?
"""


def cmd_score(args) -> None:
    conn = _connect(args.db)
    # Score any company we have gathered a signal for (i.e. probed).
    ids = [r[0] for r in conn.execute(
        f"""
        SELECT c.id FROM companies c
        JOIN website_probes p ON p.company_id = c.id
        WHERE c.id NOT IN (SELECT company_id FROM company_scores)
        {_order(args.random)}
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()]
    print(f"[score] {len(ids)} companies to score")

    done = 0
    for cid in ids:
        cur = conn.execute(_FACTS_SQL, (cid,))
        cols = [d[0] for d in cur.description]
        facts = dict(zip(cols, cur.fetchone()))
        total, breakdown = score.score_company(facts)
        enrich_db.save_score(conn, cid, total, breakdown)
        done += 1
        if done % 200 == 0:
            conn.commit()
    conn.commit()
    print(f"[score] done: {done} scored")
    conn.close()


# ---------------------------------------------------------------------------
# Batch: run every phase in sequence (for CI / scheduled runs)
# ---------------------------------------------------------------------------

def cmd_batch(args) -> None:
    """Run discover -> crawl -> extract -> ai -> contacts -> score in one pass.

    Each phase is independently incremental, so a batch both advances freshly
    discovered companies and drains any backlog from earlier phases. Email
    verification (Phase 9) is skipped by default - it needs outbound SMTP which
    GitHub-hosted runners block; enable with --with-verify on a capable host.
    """
    import argparse as _argparse

    def sub(**overrides):
        ns = _argparse.Namespace(
            db=args.db, store=args.store, limit=args.limit,
            workers=args.workers, random=args.random,
            no_patterns=False, sender="", helo="verifier.local",
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    t0 = time.time()
    print(f"=== BATCH START (limit={args.limit}/phase) ===")
    cmd_discover(sub())
    cmd_crawl(sub())
    cmd_extract(sub())
    cmd_ai(sub())
    cmd_contacts(sub())
    if args.with_verify:
        cmd_verify(sub(workers=min(args.workers, 12)))
    cmd_score(sub())
    print(f"=== BATCH DONE in {time.time()-t0:.1f}s ===")
    conn = _connect(args.db)
    # Fold the WAL back into the main .db file so a plain copy/gzip of the
    # single file captures every write (important for the CI release upload).
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stats = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM website_probes),
               (SELECT COUNT(*) FROM crawled_pages),
               (SELECT COUNT(*) FROM company_ai),
               (SELECT COUNT(*) FROM company_contacts),
               (SELECT COUNT(*) FROM company_scores)
        """
    ).fetchone()
    print(f"[totals] probes={stats[0]} pages={stats[1]} ai={stats[2]} "
          f"contacts={stats[3]} scored={stats[4]}")
    conn.close()


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB)
    common.add_argument("--store", default=DEFAULT_STORE, help="crawl HTML store root")
    common.add_argument("--limit", type=int, default=500)
    common.add_argument("--workers", type=int, default=16)
    common.add_argument("--random", action="store_true", help="sample randomly instead of by id")

    ap = argparse.ArgumentParser(description="Phases 4-10 enrichment pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "crawl", "extract", "ai", "score"):
        sub.add_parser(name, parents=[common])

    p_contacts = sub.add_parser("contacts", parents=[common])
    p_contacts.add_argument("--no-patterns", action="store_true",
                            help="don't generate standard role addresses (info@, sales@, ...)")

    p_verify = sub.add_parser("verify", parents=[common])
    p_verify.add_argument("--sender", default="", help="SMTP MAIL FROM (default: null sender)")
    p_verify.add_argument("--helo", default="verifier.local", help="SMTP HELO name")

    p_batch = sub.add_parser("batch", parents=[common])
    p_batch.add_argument("--with-verify", action="store_true",
                         help="also run SMTP verification (needs outbound port 25)")

    args = ap.parse_args()

    handlers = {
        "discover": cmd_discover, "crawl": cmd_crawl, "extract": cmd_extract,
        "ai": cmd_ai, "contacts": cmd_contacts, "verify": cmd_verify,
        "score": cmd_score, "batch": cmd_batch,
    }
    handlers[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
