#!/usr/bin/env bash
# One-time seed: publish your locally-built companies.db to the GitHub Release
# that the scheduled workflow reads from and updates.
#
# Prerequisites:
#   * gh CLI installed and authenticated (`gh auth login`)
#     - the token needs the 'workflow' scope (repo has workflow files):
#         gh auth refresh -h github.com -s workflow
#   * companies.db already built locally (python build_database.py)
#
# Usage:
#   scripts/seed_release.sh [--repo owner/name] [--db path-to-db]
#
# --repo defaults to the repo inferred from the current git remote.
set -euo pipefail

DB_TAG="db-latest"
DB="companies.db"
REPO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --db)   DB="$2";   shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      # backwards-compat: a bare argument is treated as the db path
      DB="$1"; shift ;;
  esac
done

# Pass --repo to every gh call only when explicitly provided.
REPO_ARGS=()
if [ -n "$REPO" ]; then
  REPO_ARGS=(--repo "$REPO")
fi

if [ ! -f "$DB" ]; then
  echo "error: $DB not found. Build it first: python build_database.py" >&2
  exit 1
fi

echo "Gzipping $DB ($(du -h "$DB" | cut -f1))..."
gzip -f -k "$DB"                       # creates companies.db.gz, keeps the .db

if ! gh release view "$DB_TAG" "${REPO_ARGS[@]}" >/dev/null 2>&1; then
  echo "Creating release '$DB_TAG'${REPO:+ in $REPO}..."
  gh release create "$DB_TAG" "${REPO_ARGS[@]}" \
    --title "Company database (rolling latest)" \
    --notes "Seeded from a local build; auto-updated by the enrich workflow."
fi

echo "Uploading companies.db.gz to release '$DB_TAG'..."
gh release upload "$DB_TAG" companies.db.gz --clobber "${REPO_ARGS[@]}"

echo "Done. The scheduled workflow will now restore + update this asset."
