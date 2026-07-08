#!/usr/bin/env bash
# Copies dev auth users (auth.users + auth.identities) into prod, ADDITIVELY.
# Only users whose email is not already in prod are inserted; existing prod
# accounts (and their passwords/sessions) are left untouched. Inserted users keep
# their dev UUIDs, so public.user_roles / contacts rows that reference them resolve.
# Usage: bash scripts/sync-dev-auth-users-to-prod.sh
#
# Requirements: psql in PATH, .env.dev and .env.prod with DATABASE_URL set.
# Uses the session pooler (5432). Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

read_db_url() { grep '^DATABASE_URL=' "$1" | cut -d'=' -f2- | sed "s/^['\"]//;s/['\"]$//"; }
DEV_DB_URL=$(read_db_url "$REPO_ROOT/.env.dev");  DEV_DB_URL=${DEV_DB_URL/:6543\//:5432\/}
PROD_DB_URL=$(read_db_url "$REPO_ROOT/.env.prod"); PROD_DB_URL=${PROD_DB_URL/:6543\//:5432\/}

# Non-generated columns (generated: users.confirmed_at, identities.email — must be excluded).
USER_COLS="instance_id, id, aud, role, email, encrypted_password, email_confirmed_at, invited_at, confirmation_token, confirmation_sent_at, recovery_token, recovery_sent_at, email_change_token_new, email_change, email_change_sent_at, last_sign_in_at, raw_app_meta_data, raw_user_meta_data, is_super_admin, created_at, updated_at, phone, phone_confirmed_at, phone_change, phone_change_token, phone_change_sent_at, email_change_token_current, email_change_confirm_status, banned_until, reauthentication_token, reauthentication_sent_at, is_sso_user, deleted_at, is_anonymous"
IDENT_COLS="provider_id, user_id, identity_data, provider, last_sign_in_at, created_at, updated_at, id"

TS=$(date +%Y%m%d%H%M%S)
USERS_CSV="/tmp/dev-auth-users-$TS.csv"
IDENT_CSV="/tmp/dev-auth-identities-$TS.csv"

echo "Exporting dev auth.users + auth.identities..."
psql "$DEV_DB_URL" -q -c "\copy (SELECT $USER_COLS  FROM auth.users)      TO '$USERS_CSV' CSV"
psql "$DEV_DB_URL" -q -c "\copy (SELECT $IDENT_COLS FROM auth.identities) TO '$IDENT_CSV' CSV"
echo "  users: $(wc -l < "$USERS_CSV")  identities: $(wc -l < "$IDENT_CSV")"

echo "Inserting missing users into prod (existing emails skipped)..."
psql "$PROD_DB_URL" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
CREATE TEMP TABLE stage_users      AS SELECT $USER_COLS  FROM auth.users      WITH NO DATA;
CREATE TEMP TABLE stage_identities AS SELECT $IDENT_COLS FROM auth.identities WITH NO DATA;
\copy stage_users      FROM '$USERS_CSV' CSV
\copy stage_identities FROM '$IDENT_CSV' CSV

-- Add users whose email is not already present in prod. Keep dev UUIDs.
INSERT INTO auth.users ($USER_COLS)
SELECT $USER_COLS FROM stage_users s
WHERE s.email IS NOT NULL
  AND lower(s.email) NOT IN (SELECT lower(email) FROM auth.users WHERE email IS NOT NULL)
ON CONFLICT (id) DO NOTHING;

-- Add identities for users that now exist in prod, skipping duplicates.
INSERT INTO auth.identities ($IDENT_COLS)
SELECT $IDENT_COLS FROM stage_identities si
WHERE si.user_id IN (SELECT id FROM auth.users)
ON CONFLICT (provider_id, provider) DO NOTHING;

SELECT (SELECT count(*) FROM auth.users) AS prod_users_after,
       (SELECT count(*) FROM auth.identities) AS prod_identities_after;
COMMIT;
SQL

rm -f "$USERS_CSV" "$IDENT_CSV"
echo "Done."
