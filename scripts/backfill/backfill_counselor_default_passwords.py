"""Backfill default passwords for counselors/directors that have none.

Finds every hub_admin/hub_user role whose Supabase auth user has no password set
(``auth.users.encrypted_password`` NULL/empty) and sets the default password:
email handle + the school's resource-center password (``cmm_website_password``),
or just the email handle when the school has none. Never sends an invite email.

Users who already have a password are left untouched.

Usage:
    uv run python scripts/backfill_counselor_default_passwords.py
    uv run python scripts/backfill_counselor_default_passwords.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path so `src.*` imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload
from supabase import create_client

from src.auth.hub_password import default_hub_password
from src.auth.models import UserRole
from src.config import settings
from src.db.deps import get_db

# Import all models so SQLAlchemy can resolve relationships
import src.assets.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401

# Roles treated as "counselors" (school-scoped counselor + director accounts).
COUNSELOR_ROLES = ("hub_admin", "hub_user")


def get_passwordless_auth_users(db: Session, user_ids: list[str]) -> dict[str, str]:
    """Return {user_id: email} for auth users with no password set.

    Reads auth.users directly — the only reliable source for whether a password
    hash exists. Batched to avoid a huge IN clause blowing up for large tenants.
    """
    result: dict[str, str] = {}
    batch_size = 500
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i : i + batch_size]
        rows = db.execute(
            text(
                "SELECT id::text, email FROM auth.users "
                "WHERE id::text = ANY(:ids) "
                "AND (encrypted_password IS NULL OR encrypted_password = '')"
            ),
            {"ids": batch},
        ).all()
        for uid, email in rows:
            result[uid] = email or ""
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill default passwords for counselors/directors without one"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)

    db_gen = get_db()
    db = next(db_gen)

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Backfilling default passwords for counselors without one...\n")

    # Load counselor/director roles with their school (for the resource-center password)
    role_records = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.role.in_(COUNSELOR_ROLES))
        .all()
    )
    print(f"Found {len(role_records)} counselor/director role records.")

    role_by_user_id = {str(r.user_id): r for r in role_records}
    passwordless = get_passwordless_auth_users(db, list(role_by_user_id.keys()))
    print(f"Of those, {len(passwordless)} have no password set.\n")

    updated = skipped_no_email = errors = 0

    for user_id, email in passwordless.items():
        record = role_by_user_id[user_id]
        school = record.school
        school_name = school.name if school else "(no school)"

        if not email:
            print(f"  SKIP (no email in auth.users): user_id={user_id} — {school_name}")
            skipped_no_email += 1
            continue

        school_password = school.cmm_website_password if school else None
        password = default_hub_password(email, school_password)
        source = "handle + school password" if school_password else "handle only"
        print(f"  SET password [{source}]: {email} — {school_name}")

        if args.dry_run:
            updated += 1
            continue

        try:
            supabase.auth.admin.update_user_by_id(user_id, {"password": password})
            updated += 1
        except Exception as exc:
            print(f"    ERROR updating password for {email}: {exc}")
            errors += 1

    print("\nSummary:")
    print(f"  Passwords set: {updated}")
    print(f"  Skipped (no email): {skipped_no_email}")
    print(f"  Errors: {errors}")


if __name__ == "__main__":
    main()
