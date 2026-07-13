"""Create counselor accounts from school contacts.

Uses the school's cmm_website_password as the counselor's login password.
For contacts whose email already exists, updates their password to match.

Usage:
    uv run python scripts/seed_counselors_from_contacts.py
    uv run python scripts/seed_counselors_from_contacts.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# Ensure project root is on sys.path so `src.*` imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import joinedload, Session
from supabase import create_client

from src.auth.models import UserRole
from src.config import settings
from src.db.deps import get_db
from src.schools.models import Contact, School

# Import all models so SQLAlchemy can resolve relationships
import src.assets.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401


def get_existing_counselors(db: Session, supabase) -> dict[str, str]:
    """Return {email_lower: supabase_user_id} for existing counselor/viewer roles."""
    role_records = (
        db.query(UserRole)
        .filter(UserRole.role.in_(["counselor", "viewer"]))
        .all()
    )
    result: dict[str, str] = {}
    for record in role_records:
        try:
            resp = supabase.auth.admin.get_user_by_id(str(record.user_id))
            if resp and resp.user and resp.user.email:
                result[resp.user.email.lower()] = resp.user.id
        except Exception:
            pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Create counselor accounts from school contacts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)

    db_gen = get_db()
    db = next(db_gen)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Creating counselors from school contacts...\n")

    # Get all contacts with emails, joined with their school
    contacts = (
        db.query(Contact)
        .options(joinedload(Contact.school))
        .filter(Contact.email.isnot(None))
        .filter(Contact.email != "")
        .all()
    )

    print(f"Found {len(contacts)} contacts with emails.\n")

    # Get existing counselors
    print("Checking existing counselor accounts...")
    existing_counselors = get_existing_counselors(db, supabase)
    print(f"Found {len(existing_counselors)} existing counselor/viewer accounts.\n")

    created = 0
    updated = 0
    skipped_no_password = 0
    errors = 0
    seen_emails: set[str] = set()

    for contact in contacts:
        email = contact.email.strip().lower()
        school = contact.school
        school_name = school.name if school else "Unknown"
        first_name = contact.first_name or ""
        last_name = contact.last_name or ""
        school_password = school.cmm_website_password if school else None

        if not school_password:
            print(f"  SKIP (no school password): {email} — {school_name}")
            skipped_no_password += 1
            continue

        if email in seen_emails:
            continue
        seen_emails.add(email)

        # Password = email handle + school password (e.g. rodonnellgrcmm)
        email_handle = email.split("@")[0]
        password = email_handle + school_password

        # If already exists — just update the password
        if email in existing_counselors:
            print(f"  UPDATE password: {email} — {school_name}")
            if not args.dry_run:
                try:
                    supabase.auth.admin.update_user_by_id(
                        existing_counselors[email],
                        {"password": password},
                    )
                except Exception as exc:
                    print(f"    ERROR updating password for {email}: {exc}")
                    errors += 1
                    continue
            updated += 1
            continue

        # New counselor — create user with school password
        print(f"  CREATE: {email} — {school_name} (pw: school password)")

        if args.dry_run:
            created += 1
            continue

        create_params = {
            "email": email,
            "password": password,
            "user_metadata": {
                "first_name": first_name,
                "last_name": last_name,
            },
            "email_confirm": True,
        }

        try:
            resp = supabase.auth.admin.create_user(create_params)
            if not resp or not resp.user:
                print(f"    ERROR: Failed to create auth user for {email}")
                errors += 1
                continue
            new_user = resp.user
        except Exception as exc:
            error_msg = str(exc).lower()
            if "already" in error_msg or "exists" in error_msg or "registered" in error_msg:
                # User exists in Supabase but no counselor role — find and assign
                print(f"    User exists in Supabase Auth, looking up: {email}")
                try:
                    users_resp = supabase.auth.admin.list_users()
                    new_user = next(
                        (u for u in (users_resp or []) if u.email and u.email.lower() == email),
                        None,
                    )
                    if not new_user:
                        print(f"    ERROR: Could not find existing user {email}")
                        errors += 1
                        continue
                    supabase.auth.admin.update_user_by_id(
                        new_user.id,
                        {"password": password},
                    )
                except Exception as exc2:
                    print(f"    ERROR looking up user {email}: {exc2}")
                    errors += 1
                    continue
            else:
                print(f"    ERROR creating user {email}: {exc}")
                errors += 1
                continue

        # Create or update role record
        user_id = uuid.UUID(new_user.id)
        existing_role = db.query(UserRole).filter(UserRole.user_id == user_id).first()

        if existing_role:
            existing_role.role = "counselor"
            existing_role.school_id = contact.school_id
            db.commit()
            print(f"    Updated existing role to counselor for {email}")
        else:
            role_record = UserRole(
                user_id=user_id,
                role="counselor",
                school_id=contact.school_id,
            )
            db.add(role_record)
            db.commit()
            print(f"    Created counselor role for {email}")

        created += 1

    print(f"\nSummary:")
    print(f"  Created: {created}")
    print(f"  Updated (password): {updated}")
    print(f"  Skipped (no school password): {skipped_no_password}")
    print(f"  Errors: {errors}")
    print(f"  Total contacts processed: {len(contacts)}")


if __name__ == "__main__":
    main()
