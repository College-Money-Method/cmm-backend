#!/usr/bin/env bash
# Fully replaces the production public schema (schema + data) with a copy of dev.
# DROPS and recreates the prod `public` schema, so it does NOT depend on prod
# migrations matching dev — it mirrors dev exactly. Intended as a one-shot launch step.
# Usage: bash scripts/copy-dev-to-prod-db.sh
#
# Only the `public` schema is copied. Supabase-managed schemas (auth, storage,
# realtime, ...) are intentionally left untouched — copying them across projects
# would clobber prod auth/storage and break role references.
#
# Requirements:
#   - Docker running (pg_dump + pg_restore run in a version-matched container)
#   - psql in PATH for the DDL steps (brew install libpq)
#   - .env.dev and .env.prod at the repo root with DATABASE_URL set
#
# Notes:
#   - Uses the Supabase SESSION pooler (port 5432), not the transaction pooler (6543):
#     a full schema+data restore needs a stable session.
#   - Restore runs in two phases (pre-data+data, then post-data) so the pg_trgm
#     extension can be recreated after the schema exists but before its GIN indexes.
#     pg_dump --schema=public omits CREATE EXTENSION, so we recreate it by hand.
#   - No prod migration step needed: schema comes from dev, including alembic_version.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEV_ENV_FILE="$REPO_ROOT/.env.dev"
PROD_ENV_FILE="$REPO_ROOT/.env.prod"

for f in "$DEV_ENV_FILE" "$PROD_ENV_FILE"; do
  [[ -f "$f" ]] || { echo "Error: $f not found"; exit 1; }
done

read_db_url() { grep '^DATABASE_URL=' "$1" | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//"; }

DEV_DB_URL=$(read_db_url "$DEV_ENV_FILE")
PROD_DB_URL=$(read_db_url "$PROD_ENV_FILE")

[[ -n "$DEV_DB_URL"  ]] || { echo "Error: DATABASE_URL missing in $DEV_ENV_FILE";  exit 1; }
[[ -n "$PROD_DB_URL" ]] || { echo "Error: DATABASE_URL missing in $PROD_ENV_FILE"; exit 1; }

# Force the session pooler (5432) for reliable DDL + COPY over Supavisor.
DEV_DB_URL=${DEV_DB_URL/:6543\//:5432\/}
PROD_DB_URL=${PROD_DB_URL/:6543\//:5432\/}

# Extensions dev keeps in the public schema (dropped by CASCADE, recreated manually).
PUBLIC_EXTENSIONS="pg_trgm"

# Resolve prod hostname to IPv4 so Docker can reach it via --add-host.
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

# pg_restore in Docker with prod IPv4 pinned; args passed through.
prod_restore() {
  docker run --rm \
    -v "$DUMP_DIR:$DUMP_DIR" \
    --add-host "$PROD_HOST:$PROD_IP" \
    "$PG_IMAGE" \
    pg_restore --no-owner --no-acl --exit-on-error --dbname="$PROD_DB_URL" "$@" "$DUMP_FILE"
}

echo "============================================================"
echo "  WARNING: This DROPS the prod 'public' schema and replaces"
echo "  it (schema + data) with a full copy of the dev database."
echo "============================================================"
echo ""
echo "  Dev DB:  ${DEV_DB_URL%%@*}@..."
echo "  Prod DB: ${PROD_DB_URL%%@*}@..."
echo "  Prod IP: $PROD_IP"
echo ""
read -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 0; }

# ── Step 1: Full dump of dev public schema (schema + data) ───────────────────
echo ""
echo "Step 1/5: Dumping dev database (full schema + data) via Docker..."
docker run --rm \
  -v "$DUMP_DIR:$DUMP_DIR" \
  "$PG_IMAGE" \
  pg_dump --format=custom --no-owner --no-acl --schema=public \
    --file="$DUMP_FILE" "$DEV_DB_URL"
echo "  Dump written: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"

# ── Step 2: Drop prod public schema (lock_timeout avoids hanging on live conns)
echo ""
echo "Step 2/5: Dropping prod 'public' schema..."
psql "$PROD_DB_URL" -q -c "SET lock_timeout = '30s'; DROP SCHEMA IF EXISTS public CASCADE;"
echo "  Prod 'public' schema dropped."

# ── Step 3: Restore schema + data (no indexes/constraints yet) ───────────────
echo ""
echo "Step 3/5: Restoring dev schema + data (pre-data + data)..."
prod_restore --section=pre-data --section=data

# ── Step 4: Recreate public extensions + restore Supabase default grants ─────
echo ""
echo "Step 4/5: Recreating public extensions and grants..."
EXT_SQL=""
for ext in $PUBLIC_EXTENSIONS; do
  EXT_SQL+="CREATE EXTENSION IF NOT EXISTS \"$ext\" WITH SCHEMA public; "
done
psql "$PROD_DB_URL" -q -c "
  $EXT_SQL
  GRANT USAGE, CREATE ON SCHEMA public TO postgres;
  GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
"

# ── Step 5: Restore indexes, constraints, FKs (needs extensions present) ──────
echo ""
echo "Step 5/7: Restoring indexes and constraints (post-data)..."
prod_restore --section=post-data

# ── Step 6: Bring dev auth users into prod (additive, keeps existing accounts) ─
echo ""
echo "Step 6/7: Syncing dev auth users into prod..."
bash "$SCRIPT_DIR/sync-dev-auth-users-to-prod.sh"

# ── Step 7: Remap auth user ids (public was copied, auth schema was not) ──────
# public.*.user_id columns hold dev's auth.users UUIDs. Prod has its own auth.users
# with the SAME emails but different UUIDs, so remap by email or every user hits the
# "no_school" login loop (their prod auth uid has no user_roles row).
echo ""
echo "Step 7/7: Remapping auth user ids from dev to prod (by email)..."
AUTH_MAP_CSV="$DUMP_DIR/dev-auth-map-$(date +%Y%m%d%H%M%S).csv"
psql "$DEV_DB_URL" -q -c "\copy (SELECT id::text, lower(email) FROM auth.users) TO '$AUTH_MAP_CSV' CSV"
psql "$PROD_DB_URL" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
CREATE TEMP TABLE dev_auth_map(dev_id uuid, email text);
\copy dev_auth_map FROM '$AUTH_MAP_CSV' CSV
UPDATE public.user_roles ur SET user_id = pu.id
  FROM dev_auth_map dm JOIN auth.users pu ON lower(pu.email)=dm.email
  WHERE ur.user_id = dm.dev_id;
UPDATE public.contacts c SET user_id = pu.id
  FROM dev_auth_map dm JOIN auth.users pu ON lower(pu.email)=dm.email
  WHERE c.user_id = dm.dev_id;
UPDATE public.survey_responses s SET user_id = pu.id::text
  FROM dev_auth_map dm JOIN auth.users pu ON lower(pu.email)=dm.email
  WHERE s.user_id = dm.dev_id::text;
COMMIT;
SQL
rm -f "$AUTH_MAP_CSV"

echo ""
echo "Done! Prod now mirrors dev (schema + data), auth user ids remapped."
echo "Dump file retained at: $DUMP_FILE"
