#!/usr/bin/env bash
# Copies all public-schema data from the dev database to the production database.
# Usage: bash scripts/copy-dev-to-prod-db.sh
#
# Requirements:
#   - Docker must be running (used for pg_dump + pg_restore to match server version)
#   - psql must be in PATH for the truncation step (brew install libpq)
#   - .env.dev and .env.prod at the repo root with DATABASE_URL set
#   - Use the direct Supabase connection (port 5432), not the pooler (6543)
#   - Run prod migrations FIRST: make upgrade ENV=prod  (from cmm-backend root)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEV_ENV_FILE="$REPO_ROOT/.env.dev"
PROD_ENV_FILE="$REPO_ROOT/.env.prod"

for f in "$DEV_ENV_FILE" "$PROD_ENV_FILE"; do
  [[ -f "$f" ]] || { echo "Error: $f not found"; exit 1; }
done

DEV_DB_URL=$(grep  '^DATABASE_URL=' "$DEV_ENV_FILE"  | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//")
PROD_DB_URL=$(grep '^DATABASE_URL=' "$PROD_ENV_FILE" | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//")

[[ -n "$DEV_DB_URL"  ]] || { echo "Error: DATABASE_URL missing in $DEV_ENV_FILE";  exit 1; }
[[ -n "$PROD_DB_URL" ]] || { echo "Error: DATABASE_URL missing in $PROD_ENV_FILE"; exit 1; }

# Resolve prod hostname — try IPv4 first, fall back to IPv6
# Docker Desktop 4.19+ supports IPv6; --add-host injects the resolved address
PROD_HOST=$(python3 -c "import urllib.parse; print(urllib.parse.urlparse('$PROD_DB_URL').hostname)")
PROD_IP=$(python3 -c "
import socket, sys
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        addrs = socket.getaddrinfo('$PROD_HOST', None, family)
        if addrs:
            print(addrs[0][4][0])
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
" 2>/dev/null) || { echo "Error: Could not resolve $PROD_HOST (check DNS)"; exit 1; }

PG_IMAGE="postgres:17"
DUMP_DIR="/tmp"
DUMP_FILE="$DUMP_DIR/cmm-dev-dump-$(date +%Y%m%d%H%M%S).dump"

echo "============================================================"
echo "  WARNING: This will OVERWRITE all data in PRODUCTION"
echo "  with data from the dev database."
echo "============================================================"
echo ""
echo "  Dev DB:  ${DEV_DB_URL%%@*}@..."
echo "  Prod DB: ${PROD_DB_URL%%@*}@..."
echo "  Prod IP:   $PROD_IP"
echo ""
echo "  Make sure prod migrations are up to date before continuing:"
echo "    make upgrade ENV=prod   (run from cmm-backend root)"
echo ""
read -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 0; }

# ── Step 1: Dump dev DB via Docker (version-matched, custom binary format) ───
echo ""
echo "Step 1/3: Dumping dev database via Docker..."
docker run --rm \
  -v "$DUMP_DIR:$DUMP_DIR" \
  "$PG_IMAGE" \
  pg_dump \
    --format=custom \
    --no-owner \
    --no-acl \
    --schema=public \
    --data-only \
    --file="$DUMP_FILE" \
    "$DEV_DB_URL"

echo "  Dump written: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"

# ── Step 2: Truncate prod tables via system psql (IPv4 works from host) ──────
echo ""
echo "Step 2/3: Truncating all public tables in production..."
psql "$PROD_DB_URL" -q -c "
  DO \$\$
  DECLARE r RECORD;
  BEGIN
    FOR r IN (
      SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
    ) LOOP
      EXECUTE 'TRUNCATE TABLE public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
  END \$\$;
"
echo "  Production tables cleared."

# ── Step 3: Restore via Docker with --add-host to force IPv4 for prod ────────
echo ""
echo "Step 3/3: Restoring dev data into production..."
docker run --rm \
  -v "$DUMP_DIR:$DUMP_DIR" \
  --add-host "$PROD_HOST:$PROD_IP" \
  "$PG_IMAGE" \
  pg_restore \
    --no-owner \
    --no-acl \
    --schema=public \
    --data-only \
    --dbname="$PROD_DB_URL" \
    "$DUMP_FILE"

echo ""
echo "Done! Dev data has been copied to production."
echo "Dump file retained at: $DUMP_FILE"
