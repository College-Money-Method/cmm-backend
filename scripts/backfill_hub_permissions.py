#!/usr/bin/env python3
"""
Backfill hub_permission on user_roles rows from Airtable contact roles.

Reads the "Role" field from Airtable Contacts:
  - "Director"  → hub_permission = 'admin'
  - "Counselor" → hub_permission = 'user'

Matching path: Airtable record → contacts.airtable_id (or email) → email
              → Supabase auth user_id → user_roles.user_id

Usage (from project root):
  uv run python scripts/backfill_hub_permissions.py --dry-run
  uv run python scripts/backfill_hub_permissions.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyairtable import Api
from supabase import create_client

from src.config import settings
from src.db.base import get_engine
from sqlalchemy import text

# Airtable role → hub_permission value
ROLE_MAP: dict[str, str] = {
    "Director": "admin",
    "Counselor": "user",
}


def _build_supabase_email_map(supabase) -> dict[str, str]:
    """Return email.lower() → user_id for all Supabase auth users (paginated)."""
    email_to_user_id: dict[str, str] = {}
    page = 1
    while True:
        response = supabase.auth.admin.list_users(page=page, per_page=1000)
        users = response if isinstance(response, list) else []
        for user in users:
            if user.email:
                email_to_user_id[user.email.lower()] = user.id
        if len(users) < 1000:
            break
        page += 1
    return email_to_user_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill hub_permission on user_roles from Airtable contact roles"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print updates without writing to DB")
    args = parser.parse_args()

    # ── Validate config ────────────────────────────────────────────────────────
    if not settings.airtable_api_key:
        print("Error: AIRTABLE_API_KEY is not set", file=sys.stderr)
        return 1
    if not settings.airtable_base_id:
        print("Error: AIRTABLE_BASE_ID is not set", file=sys.stderr)
        return 1
    if not settings.database_url:
        print("Error: DATABASE_URL is not set", file=sys.stderr)
        return 1
    supabase_key = settings.supabase_service_role_key or settings.supabase_key
    if not supabase_key:
        print("Error: SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) is not set", file=sys.stderr)
        return 1

    # ── 1. Fetch all Contacts from Airtable ───────────────────────────────────
    print("Fetching Contacts from Airtable...")
    api = Api(settings.airtable_api_key)
    at_records = api.table(settings.airtable_base_id, "Contacts").all()
    print(f"  {len(at_records)} Airtable contact records fetched")

    # ── 2. Build Airtable lookups: airtable_id → (role, email), email → role ──
    at_id_to_role_email: dict[str, tuple[str, str | None]] = {}
    at_email_to_hub_perm: dict[str, str] = {}

    skipped_no_role = 0
    for rec in at_records:
        fields = rec.get("fields", {})
        role_raw: str | None = (fields.get("Role") or "").strip() or None
        email_raw: str | None = (fields.get("Email") or "").strip().lower() or None

        hub_perm = ROLE_MAP.get(role_raw or "") if role_raw else None
        if not hub_perm:
            skipped_no_role += 1
            continue

        at_id_to_role_email[rec["id"]] = (hub_perm, email_raw)
        if email_raw:
            # Last writer wins if same email appears with different roles — Director takes priority
            existing = at_email_to_hub_perm.get(email_raw)
            if existing != "admin":  # admin is highest privilege
                at_email_to_hub_perm[email_raw] = hub_perm

    actionable = len(at_id_to_role_email)
    print(
        f"  {actionable} contacts with actionable roles "
        f"({skipped_no_role} skipped — no Director/Counselor role)"
    )
    admin_count = sum(1 for v in at_id_to_role_email.values() if v[0] == "admin")
    user_count = sum(1 for v in at_id_to_role_email.values() if v[0] == "user")
    print(f"  admin (Director): {admin_count}, user (Counselor): {user_count}")

    # ── 3. Load contacts from our DB ──────────────────────────────────────────
    print("\nLoading contacts from DB...")
    engine = get_engine()
    with engine.connect() as conn:
        db_contacts = conn.execute(
            text("SELECT id, airtable_id, email FROM contacts")
        ).fetchall()

    print(f"  {len(db_contacts)} DB contacts loaded")

    # Build DB lookup maps
    db_at_id_to_email: dict[str, str] = {}  # airtable_id → email
    db_email_set: set[str] = set()

    for row in db_contacts:
        email = (row.email or "").strip().lower() or None
        at_id = row.airtable_id or None
        if at_id and email:
            db_at_id_to_email[at_id] = email
        if email:
            db_email_set.add(email)

    # ── 4. Build final email → hub_permission map (resolve via DB contacts) ───
    # Priority: match by airtable_id first, then fall back to email match
    email_to_hub_perm: dict[str, str] = {}

    for at_id, (hub_perm, at_email) in at_id_to_role_email.items():
        # Try airtable_id match
        db_email = db_at_id_to_email.get(at_id)
        if db_email:
            # airtable_id matched → use DB email (more reliable)
            existing = email_to_hub_perm.get(db_email)
            if existing != "admin":
                email_to_hub_perm[db_email] = hub_perm
            continue
        # Fall back to email match
        if at_email and at_email in db_email_set:
            existing = email_to_hub_perm.get(at_email)
            if existing != "admin":
                email_to_hub_perm[at_email] = hub_perm

    print(f"  {len(email_to_hub_perm)} unique emails resolved to hub_permission values")

    if not email_to_hub_perm:
        print("\nNo emails matched between Airtable and DB contacts — nothing to update.")
        return 0

    # ── 5. Load all Supabase auth users (email → user_id) ────────────────────
    print("\nLoading Supabase auth users...")
    supabase = create_client(settings.supabase_url, supabase_key)
    try:
        supabase_email_map = _build_supabase_email_map(supabase)
    except Exception as exc:
        print(f"Error: failed to list Supabase users — {exc}", file=sys.stderr)
        return 1
    print(f"  {len(supabase_email_map)} Supabase users loaded")

    # ── 6. Build list of updates: (user_id, hub_permission) ──────────────────
    updates: list[dict] = []
    not_in_supabase: list[str] = []

    for email, hub_perm in email_to_hub_perm.items():
        user_id = supabase_email_map.get(email)
        if not user_id:
            not_in_supabase.append(email)
            continue
        updates.append({"user_id": user_id, "hub_permission": hub_perm})

    print(f"\n  {len(updates)} user_role rows to update")
    print(f"  {len(not_in_supabase)} emails not found in Supabase auth")
    if not_in_supabase:
        for e in not_in_supabase[:10]:
            print(f"    [warn] not in Supabase: {e}")
        if len(not_in_supabase) > 10:
            print(f"    ... and {len(not_in_supabase) - 10} more")

    if args.dry_run:
        print("\nDry run — sample of planned updates (up to 10):")
        for u in updates[:10]:
            print(f"  user_id={u['user_id']}  hub_permission={u['hub_permission']}")
        if len(updates) > 10:
            print(f"  ... and {len(updates) - 10} more")
        print("\nDry run complete — no DB changes written.")
        return 0

    # ── 7. Apply updates ──────────────────────────────────────────────────────
    updated = not_found_in_db = 0
    with engine.begin() as conn:
        for u in updates:
            result = conn.execute(
                text(
                    """
                    UPDATE user_roles
                       SET hub_permission = :hub_permission
                     WHERE user_id = :user_id
                    """
                ),
                u,
            )
            if result.rowcount:
                updated += 1
            else:
                not_found_in_db += 1
                print(f"  [warn] user_roles row not found for user_id={u['user_id']}")

    print(
        f"\nDone — {updated} updated, "
        f"{not_found_in_db} not found in user_roles, "
        f"{len(not_in_supabase)} not in Supabase auth."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
