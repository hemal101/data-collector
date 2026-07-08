# Master Company Database

A single source of truth built from `rawData.csv` (Startup India export,
~396k rows). Raw records are **normalized** and **deduplicated** before landing
in a SQLite database. Listmonk (or any downstream mailer) should only ever pull
subscribers *from this database*, never from the raw CSV.

> **Emails are intentionally out of scope** for this phase (Phases 1–3).

## Quick start

**Phases 1-3** (build the master DB) — pure Python standard library:

```bash
python3 build_database.py            # reads rawData.csv -> writes companies.db
python3 tests/test_pipeline.py       # 31 tests
```

Useful flags: `--input`, `--db`, `--startups-only`, `--limit N`.

**Phases 4-6** (enrich from the live web) — need a few dependencies:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_enrich.py                 # 16 tests

.venv/bin/python run_enrichment.py discover --limit 500 --random   # Phase 4
.venv/bin/python run_enrichment.py crawl    --limit 500            # Phase 5
.venv/bin/python run_enrichment.py extract  --limit 500            # Phase 6
```

**Phases 7-10** (AI enrichment, contacts, verification, scoring):

```bash
.venv/bin/python run_enrichment.py ai       --limit 500   # Phase 7
.venv/bin/python run_enrichment.py contacts --limit 500   # Phase 8
.venv/bin/python run_enrichment.py verify   --limit 500   # Phase 9 (needs port 25)
.venv/bin/python run_enrichment.py score    --limit 500   # Phase 10
```

Or run everything except verification in one pass (used by CI):

```bash
.venv/bin/python run_enrichment.py batch --limit 500 --random
```

Each enrichment subcommand is **incremental and resumable** — it only processes
companies not yet done, so you can stop/restart and grow coverage over time.
Common flags: `--limit N`, `--workers N`, `--random`, `--store DIR`.

## Results (full dataset)

| Metric | Value |
|---|---|
| Raw rows read | 395,935 |
| Duplicates removed | ~25,850 |
| **Master companies** | **~370,082** |
| With domain | ~175k |
| With valid CIN | ~194k |
| With valid phone | ~129k |

## Project layout

```
build_database.py        Phases 1-3 orchestrator: CSV -> normalize -> dedup -> load
run_enrichment.py        Phases 4-6 orchestrator: discover / crawl / extract
requirements.txt         Deps for Phases 4-6 (Phases 1-3 are stdlib-only)
pipeline/
  normalize.py           Phase 2: pure field-normalization functions
  dedup.py               Phase 3: union-find deduplication + record merging
  db.py                  Phase 1: companies schema + bulk load
  enrich_db.py           Phases 4-6: enrichment schema + writers
  discovery.py           Phase 4: HTTP probe + DNS/MX/SPF/DMARC/hosting
  crawl.py               Phase 5: fetch public pages, store gzipped HTML
  extract.py             Phase 6: parse HTML -> facts, contacts, socials, tech
  ai_enrich.py           Phase 7: AI/heuristic segmentation attributes
  llm.py                 Pluggable LLM provider (OpenAI/Anthropic) or None
  contacts.py            Phase 8: classify + generate contact emails
  verify.py              Phase 9: SMTP/MX/disposable email verification
  score.py               Phase 10: transparent lead scoring
tests/
  test_pipeline.py       Phases 1-3 tests (31)
  test_enrich.py         Phases 4-6 tests (16)
  test_phases710.py      Phases 7-10 tests (15)
scripts/
  seed_release.sh        One-time: push local DB to the GitHub Release
  status.py              Markdown progress report (used by CI job summary)
.github/workflows/
  enrich.yml             Scheduled batch enrichment (every 5 min)
crawl_store/             Gzipped crawled HTML, sharded <xx>/<domain>/<page>.html.gz
```

---

## Phase 1 — The `companies` schema

`companies` is the master table. Every column requested in the spec is present,
plus `role` (so non-startup entities can be filtered later) and `duplicate_count`
(how many raw records were merged in).

| Column | Notes |
|---|---|
| `id` | Auto-increment primary key |
| `startup_india_id` | Source id from the raw `id` column |
| `name` | Cleaned company name |
| `website` | Canonical URL `https://host[/path]` |
| `domain` | Bare registrable host, e.g. `abc.com` |
| `industry` / `sector` / `stage` | From `industries` / `sectors` / `stages` |
| `city` / `state` | Normalized to canonical Indian place names |
| `registration_date` | ISO `YYYY-MM-DD` |
| `cin` | Validated Corporate Identification Number (else NULL) |
| `dpiit_certified` | Boolean (0/1); TRUE if *any* merged source was certified |
| `status` | Company status (Active, Strike Off, ...) |
| `phone` | Canonical `+91XXXXXXXXXX` |
| `role` | Startup / Investor / Incubator / Mentor / ... |
| `duplicate_count` | Number of raw records merged into this company |
| `created_at` / `updated_at` | UTC ISO timestamps |

A companion **`company_sources`** table maps every raw `startup_india_id` to the
company it was merged into, so deduplication is fully traceable and no source id
is ever lost.

---

## Phase 2 — Normalization

Every rule lives in `pipeline/normalize.py` as a small, unit-tested pure
function. Highlights:

- **Websites → domains** (the spec's example flow):
  `https://abc.com/` → `https://abc.com` → `abc.com`.
  Adds a missing scheme, lower-cases the host, strips `www.`, ports, userinfo,
  trailing slashes/dots, and rejects placeholder values (`Not Available`, `NA`, …).
- **Company names**: whitespace collapsed, ALL-CAPS titled-cased, and legal
  suffixes standardized (`Pvt Ltd`, `Pvt. Ltd.`, `Private Limited` → `Private Limited`).
- **States**: mapped to the 36 canonical Indian states/UTs (`Orissa` → `Odisha`,
  `Pondicherry` → `Puducherry`, `TAMIL NADU` → `Tamil Nadu`, …).
- **Cities**: title-cased with common renames (`Bangalore` → `Bengaluru`,
  `Gurgaon` → `Gurugram`, `Bombay` → `Mumbai`, …).
- **Phone numbers**: reduced to a canonical `+91XXXXXXXXXX`, handling leading
  `0`, `91`/`+91` country codes, separators, and stray extra digits. Anything
  that isn't a plausible 10-digit Indian number becomes NULL.
- **CIN**: upper-cased and validated against the 21-char CIN format; invalid
  values (e.g. `ABZ-8755`) are dropped rather than stored as fake CINs.
- **Dates**: `dd-mm-yyyy` → ISO, with the `registeredOn` epoch as a fallback.

---

## Phase 3 — Deduplication

The same company appears across many datasets. `pipeline/dedup.py` links records
that share **any** strong identifier and treats each connected group as one
company (union-find, so links are transitive: A~B and B~C ⇒ one company).

Match keys, strongest first:

1. `startup_india_id` (exact)
2. `cin` (exact, validated)
3. `domain` (exact)
4. `website` (exact, normalized URL)
5. **company name key** (suffix-normalized) — *gated by matching state*

Example: `ABC Pvt Ltd`, `ABC Private Limited`, `ABC Pvt. Ltd.` → key
`abc private limited` → **one company**.

### Guardrails against over-merging

Naive matching wrongly fuses unrelated companies, so the pipeline defends against
the three failure modes we actually saw in this data:

- **Name matching is gated by state** — two identically-named companies in
  different states are *not* merged (unless a stronger key like CIN links them).
- **Generic/placeholder domains are ignored** — shared hosts such as
  `linkedin.com`, `indiamart.com`, `bit.ly`, `zaubacorp.com`, `nowebsite.com`
  (and their subdomains) never link two companies.
- **Self-tuning shared-domain detection** — any domain the data itself shows
  attached to ≥4 distinct company names or ≥2 distinct CINs is auto-excluded as
  a directory/aggregator (this caught ~1,100 domains like `dnb.com`,
  `msmemart.com`, `g.page` without a hand-maintained list).

When a cluster is merged, the canonical record is built from the most complete
source, coalescing missing fields from the others; `dpiit_certified` is TRUE if
any source was certified, and the earliest known registration date wins.

---

## Phase 4 — Website discovery

For every company that has a domain, `run_enrichment.py discover` records
reachability and infrastructure into `website_probes` and `dns_records`:

- **Probe chain**: resolved? → redirect? → https? → alive? → favicon → title →
  robots.txt (tries `https://` first, falls back to `http://`, follows
  redirects, tolerates broken TLS, captures the `Server` header).
- **DNS / mail**: A, NS, MX records; SPF (`v=spf1`) and DMARC (`_dmarc` TXT).
- **Hosting provider**: inferred from GitHub-Pages IPs, NS/reverse-DNS/`Server`
  signatures (Cloudflare, AWS, GoDaddy, Vercel, Netlify, Wix, …), falling back
  to the raw server banner.

Runs concurrently (thread pool, `--workers`), commits every 50 rows.

## Phase 5 — Crawl the public website

`run_enrichment.py crawl` visits reachable sites and stores **raw HTML only** —
no parsing yet. It fetches the homepage, then follows same-site links to the
standard pages: **about, contact, team, careers, privacy, terms, blog**. Each
page is gzipped to `crawl_store/<xx>/<domain>/<page_type>.html.gz` and indexed
in `crawled_pages`. Off-site links, `mailto:`/`tel:`/`#`, and oversized pages
are skipped.

## Phase 6 — Extract everything

`run_enrichment.py extract` parses the stored HTML offline (re-runnable without
the network) into structured tables:

- `company_enrichment`: extracted name, description, logo, founded year,
  employee count, address (via meta tags, OpenGraph, and schema.org JSON-LD).
- `company_emails` / `company_phones`: from `mailto:`/`tel:` and body text
  (phones normalized to `+91…`, junk/image/example emails filtered).
- `company_socials`: LinkedIn, Twitter/X, Instagram, Facebook, YouTube, GitHub
  (share/intent links and bare non-profile URLs dropped).
- `company_tech`: technology fingerprinting with evidence, covering the
  requested set — **WordPress, Shopify, React, Next.js, Laravel, Cloudflare,
  Google Analytics** — plus Wix, Squarespace, Webflow, Vue, Angular, GTM,
  Facebook Pixel, jQuery, Bootstrap, and more.

This powers segmentation later (e.g. "DPIIT-certified Shopify stores in
Karnataka with a working MX record").

## Phase 7 — AI enrichment

`run_enrichment.py ai` generates marketing-segmentation attributes per company
into `company_ai`: short description, industry, sub-industry, ICP, business type
(B2B/B2C/Marketplace), target market, and keywords.

It is **LLM-ready but works without a key**: if `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY` is set it asks the model for structured JSON; otherwise it
falls back to a deterministic heuristic built from the Startup India taxonomy +
crawled description + detected tech. Set a key (and optionally `OPENAI_MODEL` /
`ANTHROPIC_MODEL`) to upgrade quality with zero code changes.

## Phase 8 — Contact discovery

`run_enrichment.py contacts` turns crawled emails into a classified
`company_contacts` table. Each email is typed (support / sales / founder / ceo /
hr / marketing / info / contact / general) and carries provenance:
**source** (crawl vs generated pattern), **confidence**, **found_on**,
**verified** status, and **last_checked**. Standard role addresses
(`info@`, `contact@`, `sales@`, `support@`, `hello@`) are also generated per
domain so Phase 9 can test them; off-domain addresses (e.g. a founder's gmail)
get reduced confidence.

## Phase 9 — Email verification

`run_enrichment.py verify` labels each contact `verified` / `invalid` /
`catch-all` / `disposable` / `unknown` via: syntax check → disposable-domain
list → MX lookup → SMTP `RCPT` probe (null sender) with **catch-all detection**
(a random address is probed too; if it's also accepted the domain is catch-all).
Per-domain MX and catch-all results are cached. **Never import `invalid`** (and
in practice treat `disposable` as unusable, `catch-all`/`unknown` as low trust).

> Requires outbound **SMTP port 25**, which GitHub-hosted runners block — run
> this locally or on a self-hosted runner, not in the default CI.

## Phase 10 — Company scoring

`run_enrichment.py score` computes a transparent lead score into
`company_scores` (with a JSON breakdown), from signals gathered across all
phases:

| Factor | Points |
|---|---:|
| Website | 10 |
| Email | 20 |
| LinkedIn | 5 |
| Verified Email | 30 |
| HTTPS | 5 |
| Active Website | 5 |
| Contact Page | 5 |
| Company Description | 5 |
| Social Presence | 10 |

(The listed factors sum to 95; the total is clamped to a 0-100 range.)

## Automation — scheduled enrichment via GitHub Actions

`.github/workflows/enrich.yml` runs the pipeline continuously on GitHub-hosted
runners:

- **Every 5 minutes** (`cron` — GitHub's minimum; per-minute isn't possible on
  hosted runners) plus manual `workflow_dispatch` with `limit`/`workers` inputs.
- Each run **restores** the DB from a gzipped **GitHub Release asset**
  (tag `db-latest`), runs `batch` on the next ~500 companies per phase, then
  **re-uploads** the updated DB and writes a progress table to the job summary.
- A `concurrency` group prevents overlapping runs from corrupting the DB.
- **Email verification is skipped** (port 25 blocked on hosted runners).

The DB isn't committed to git (it's >100MB — over GitHub's file limit — and
grows), so it lives as a release asset. **Seed it once** from your local build:

```bash
gh auth login
python build_database.py            # if you haven't already
scripts/seed_release.sh             # creates release 'db-latest' + uploads companies.db.gz
```

After seeding, the schedule takes over and grows coverage automatically. Pull
the latest data anytime with `gh release download db-latest -p companies.db.gz`.

### Other options considered

- **Self-hosted runner** (per-minute, DB stays on local disk, verification
  works) — best if you have an always-on machine and want true 1-minute cadence.
- **External hosted libSQL/Turso** — CI writes directly with no file shuffling;
  the cleanest path at large scale, at the cost of an account + a small DB-layer
  change.

## Feeding Listmonk (next phase)

This database is the gate: enrich + qualify companies here first, then push only
the qualified subset (with emails, once that phase is added) into Listmonk.
`company_sources` lets you reconcile back to any original Startup India record.
