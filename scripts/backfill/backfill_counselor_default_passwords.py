"""Backfill default passwords for counselors/directors that have none.

Finds every hub_admin/hub_user role whose Supabase auth user has no password set
(``auth.users.encrypted_password`` NULL/empty) and sets the default password:
email handle + the school's resource-center password (``cmm_website_password``),
or just the email handle when the school has none. Never sends an invite email.

By default, users who already have a password are left untouched. Pass
``--reset-all`` to force EVERY counselor/director password back to the default,
including accounts whose password was changed (e.g. by testers before launch).

Usage:
    uv run python scripts/backfill_counselor_default_passwords.py
    uv run python scripts/backfill_counselor_default_passwords.py --dry-run
    uv run python scripts/backfill_counselor_default_passwords.py --reset-all --dry-run
    uv run python scripts/backfill_counselor_default_passwords.py --reset-all
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


def get_auth_user_emails(
    db: Session, user_ids: list[str], *, only_passwordless: bool
) -> dict[str, str]:
    """Return {user_id: email} for the given auth users.

    Reads auth.users directly — the only reliable source for whether a password
    hash exists. When ``only_passwordless`` is True, restricts to users with no
    password set (``encrypted_password`` NULL/empty); otherwise returns every
    matching auth user regardless of password state. Batched to avoid a huge IN
    clause blowing up for large tenants.
    """
    passwordless_clause = (
        " AND (encrypted_password IS NULL OR encrypted_password = '')"
        if only_passwordless
        else ""
    )
    result: dict[str, str] = {}
    batch_size = 500
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i : i + batch_size]
        rows = db.execute(
            text(
                "SELECT id::text, email FROM auth.users "
                "WHERE id::text = ANY(:ids)" + passwordless_clause
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
    parser.add_argument(
        "--reset-all",
        action="store_true",
        help="Reset EVERY counselor/director password to default, even if one is already set",
    )
    args = parser.parse_args()

    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)

    db_gen = get_db()
    db = next(db_gen)

    prefix = "[DRY RUN] " if args.dry_run else ""
    scope = "ALL counselors (reset)" if args.reset_all else "counselors without one"
    print(f"{prefix}Backfilling default passwords for {scope}...\n")

    # Load counselor/director roles with their school (for the resource-center password)
    role_records = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.role.in_(COUNSELOR_ROLES))
        .all()
    )
    print(f"Found {len(role_records)} counselor/director role records.")

    role_by_user_id = {str(r.user_id): r for r in role_records}
    targets = get_auth_user_emails(
        db, list(role_by_user_id.keys()), only_passwordless=not args.reset_all
    )
    if args.reset_all:
        print(f"Resetting passwords for all {len(targets)} of them.\n")
    else:
        print(f"Of those, {len(targets)} have no password set.\n")

    updated = skipped_no_email = errors = 0

    for user_id, email in targets.items():
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
