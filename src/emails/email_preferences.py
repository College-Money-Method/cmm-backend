"""Keeping the two contact opt-ins and the unsubscribe suppression row in sync.

``EmailSuppression`` blocks EVERY send to an address, whatever the opt-ins say,
so the two have to move together: a contact who turns both opt-ins off is
suppressed, and one who turns either back on must have that suppression lifted
or they would keep receiving nothing while the Hub shows them as opted in.

Only ``reason="unsubscribe"`` rows are touched. Bounce and complaint
suppressions are deliverability decisions made by the receiving mail server —
a recipient re-opting in cannot clear those.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.emails.models import EmailSuppression
from src.schools.models import Contact

UNSUBSCRIBE_REASON = "unsubscribe"


def sync_unsubscribe_suppression(db: Session, contact: Contact) -> None:
    """Add or remove ``contact``'s unsubscribe suppression to match its opt-ins.

    Call after assigning ``auto_emails`` / ``broadcast_emails``; the caller owns
    the commit.
    """
    if not contact.email:
        return

    wants_nothing = not contact.auto_emails and not contact.broadcast_emails
    existing = (
        db.query(EmailSuppression).filter(EmailSuppression.email == contact.email).first()
    )

    if wants_nothing:
        if existing is None:
            # Query-first (not an upsert): keeps this portable across the
            # Postgres prod DB and the SQLite DB used in tests, and a race here
            # is low-stakes (single-click, low-concurrency flow) unlike the
            # bounce/complaint webhook's on_conflict_do_nothing.
            db.add(EmailSuppression(email=contact.email, reason=UNSUBSCRIBE_REASON))
        return

    if existing is not None and existing.reason == UNSUBSCRIBE_REASON:
        db.delete(existing)
