"""Seed super_admin role records for CMM team members.

Looks up each email in Supabase Auth (creating the user if they don't exist),
then upserts a user_roles row with role='super_admin'.

Usage:
    uv run python scripts/seed_super_admins.py
    uv run python scripts/seed_super_admins.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# Ensure project root is on sys.path so `src.*` imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.dialects.postgresql import insert as pg_insert
from supabase import create_client

from src.auth.models import UserRole
from src.config import settings
from src.db.deps import get_db

SUPER_ADMIN_EMAILS = [
    "vu.nguyen@collegemoneymethod.com",
    "paul.martin@collegemoneymethod.com",
    "caroling.lee@collegemoneymethod.com",
    "tu.dang@collegemoneymethod.com"    
]


def get_or_create_user(supabase, email: str, dry_run: bool) -> str | None:
    """Return the Supabase user_id for the given email, creating the user if needed."""
    # Try listing users to find by email (pages through up to 1000 users)
    try:
        page = 1
        while True:
            response = supabase.auth.admin.list_users(page=page, per_page=1000)
            users = response if isinstance(response, list) else []
            for user in users:
                if user.email and user.email.lower() == email.lower():
                    print(f"  Found existing user: {email} ({user.id})")
                    return user.id
            if len(users) < 1000:
                break
            page += 1
    except Exception as exc:
        print(f"  Warning: could not list users — {exc}")

    # User not found — create them with an invite
    print(f"  User not found, creating invite for: {email}")
    if dry_run:
        print(f"  [dry-run] Would invite {email}")
        return None

    try:
        resp = supabase.auth.admin.invite_user_by_email(email)
        if resp and resp.user:
            print(f"  Invited {email} ({resp.user.id})")
            return resp.user.id
    except Exception as exc:
        # If they already exist but were missed above, try create_user
        try:
            resp = supabase.auth.admin.create_user({
                "email": email,
                "email_confirm": True,
            })
            if resp and resp.user:
                print(f"  Created {email} ({resp.user.id})")
                return resp.user.id
        except Exception as exc2:
            print(f"  ERROR creating user {email}: {exc2}")
            return None

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed super_admin roles")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)

    db_gen = get_db()
    db = next(db_gen)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Seeding super_admin roles...\n")

    for email in SUPER_ADMIN_EMAILS:
        print(f"Processing: {email}")
        user_id_str = get_or_create_user(supabase, email, args.dry_run)

        if not user_id_str:
            if not args.dry_run:
                print(f"  SKIP — could not resolve user_id for {email}\n")
            continue

        user_id = uuid.UUID(user_id_str)

        if args.dry_run:
            print(f"  [dry-run] Would upsert user_roles: user_id={user_id}, role=super_admin\n")
            continue

        # Upsert: insert or update role to super_admin
        stmt = (
            pg_insert(UserRole)
            .values(user_id=user_id, role="super_admin", school_id=None)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"role": "super_admin", "school_id": None},
            )
        )
        db.execute(stmt)
        db.commit()
        print(f"  Upserted super_admin role for {email}\n")

    print("Done.")


if __name__ == "__main__":
    main()
