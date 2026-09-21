"""
One-time backfill: classify guest_contacts rows that predate the spam guard.

Migration 0127 adds is_spam defaulting to false, so every historical row lands in
the inbox regardless of what it is. This runs the same heuristics the endpoint
now applies and quarantines the matches, which is what clears the junk already
sitting in /admin/messages.

Only rows never touched by an admin are considered. Both admin reasons —
``marked_by_admin`` and ``restored_by_admin`` — record a human decision and are
left exactly as they are, which is also what makes the script safe to re-run: a
row an admin pulled back into the inbox will not be quarantined again.

Usage:
    python -m scripts.backfill.backfill_guest_contact_spam_flags          # report only
    python -m scripts.backfill.backfill_guest_contact_spam_flags --apply  # write
"""

from __future__ import annotations

import os
import sys

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import src.guest_contacts.models  # noqa: F401

from sqlalchemy import or_

from src.db.base import get_session_factory
from src.guest_contacts.models import GuestContact
from src.guest_contacts.spam_detection import ADMIN_DECISIONS, detect_spam


def main() -> None:
    apply_changes = "--apply" in sys.argv

    session_factory = get_session_factory()
    db = session_factory()
    try:
        rows = (
            db.query(GuestContact)
            .filter(
                or_(
                    GuestContact.spam_reason.is_(None),
                    GuestContact.spam_reason.notin_(ADMIN_DECISIONS),
                )
            )
            .order_by(GuestContact.created_at.desc())
            .all()
        )

        flagged = 0
        for row in rows:
            reason = detect_spam(
                first_name=row.first_name,
                last_name=row.last_name,
                email=row.email,
                school_name=row.school_name,
                message=row.message,
                honeypot=None,  # not recorded historically
            )
            if reason is None or row.is_spam:
                continue
            flagged += 1
            preview = " ".join((row.message or "").split())[:60]
            print(f"  {reason:22} {row.email:36} {preview}")
            if apply_changes:
                row.is_spam = True
                row.spam_reason = reason

        print(f"\n{flagged} of {len(rows)} rows classified as spam.")
        if apply_changes:
            db.commit()
            print("Applied.")
        else:
            print("Dry run — re-run with --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
