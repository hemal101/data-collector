#!/usr/bin/env bash
# One-time seed: publish your locally-built companies.db to the GitHub Release
# that the scheduled workflow reads from and updates.
#
# Prerequisites:
#   * gh CLI installed and authenticated (`gh auth login`)
#   * run from inside the repo (a GitHub remote must be configured)
#   * companies.db already built locally (python build_database.py)
#
# Usage: scripts/seed_release.sh [path-to-db]
set -euo pipefail

DB_TAG="db-latest"
DB="${1:-companies.db}"

if [ ! -f "$DB" ]; then
  echo "error: $DB not found. Build it first: python build_database.py" >&2
  exit 1
fi

echo "Gzipping $DB ($(du -h "$DB" | cut -f1))..."
gzip -f -k "$DB"                       # creates companies.db.gz, keeps the .db

if ! gh release view "$DB_TAG" >/dev/null 2>&1; then
  echo "Creating release '$DB_TAG'..."
  gh release create "$DB_TAG" \
    --title "Company database (rolling latest)" \
    --notes "Seeded from a local build; auto-updated by the enrich workflow."
fi

echo "Uploading companies.db.gz to release '$DB_TAG'..."
gh release upload "$DB_TAG" companies.db.gz --clobber

echo "Done. The scheduled workflow will now restore + update this asset."
