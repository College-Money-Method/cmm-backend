"""
One-time backfill: file pre-existing guest_contacts rows under the Parents tab.

Migration 0133 adds is_parent defaulting to false, so every historical row lands
in the inbox whoever wrote it. This runs the same classifier the endpoint now
applies and moves the families across, which is what leaves the inbox holding
the schools and counsellors the form was built for.

Rows an admin has already ruled on are skipped, in both directions:
``marked_by_admin`` and ``restored_by_admin`` each record a human decision. That
is also what makes the script safe to re-run — a row an admin sent back to the
inbox will not be refiled on the next pass.

Quarantined rows are skipped too. Spam outranks audience, so re-reading junk for
family vocabulary would only put it in a second tab.

Usage:
    uv run python -m scripts.backfill.backfill_guest_contact_parent_flags --env-file .env.prod
    uv run python -m scripts.backfill.backfill_guest_contact_parent_flags --env-file .env.prod --apply
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the flags (default: report)")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    args = parser.parse_args()

    # Load the chosen environment before importing anything that reads settings:
    # src.config binds DATABASE_URL at import time, so loading afterwards is
    # ignored and the script quietly works on whatever .env points at.
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)
    # Say it out loud: the default is .env, and a run against the wrong database
    # looks exactly like a run that found nothing to do.
    print(f"Environment: {args.env_file}\n")

    from sqlalchemy import or_

    import src.guest_contacts.models  # noqa: F401
    from src.db.base import get_session_factory
    from src.guest_contacts.models import GuestContact
    from src.guest_contacts.parent_detection import ADMIN_DECISIONS, detect_parent

    session_factory = get_session_factory()
    db = session_factory()
    try:
        rows = (
            db.query(GuestContact)
            .filter(
                GuestContact.is_spam.is_(False),
                or_(
                    GuestContact.parent_reason.is_(None),
                    GuestContact.parent_reason.notin_(ADMIN_DECISIONS),
                ),
            )
            .order_by(GuestContact.created_at.desc())
            .all()
        )

        filed = 0
        for row in rows:
            reason = detect_parent(role=row.role, message=row.message)
            if reason is None or row.is_parent:
                continue
            filed += 1
            preview = " ".join((row.message or "").split())[:60]
            print(f"  {reason:22} {row.email:36} {preview}")
            if args.apply:
                row.is_parent = True
                row.parent_reason = reason

        print(f"\n{filed} of {len(rows)} rows filed under Parents.")
        if args.apply:
            db.commit()
            print("Applied.")
        else:
            print("Dry run — re-run with --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
