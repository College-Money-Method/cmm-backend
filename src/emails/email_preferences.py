"""Contact email opt-ins: their default on new hub access, and the unsubscribe
suppression row they must stay in sync with.

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


def apply_default_opt_ins(db: Session, contact: Contact) -> bool:
    """Opt ``contact`` into both email streams as Counselor Hub access is granted.

    CMM's stance is opt-out, not opt-in: a counselor handed hub access is there
    to run workshops, so they start subscribed to both the scheduler automations
    and admin broadcasts, and stay that way until they turn either off
    themselves on the Hub Team page.

    Only ever applied to a contact who is subscribed to nothing, which is what
    every deliberate opt-out looks like from here. Hub access can be revoked and
    re-granted (the contacts row and its opt-ins outlive the login), so this runs
    again on people who have already made a choice: someone who kept broadcasts
    but turned workshop mail off must not have it switched back on behind them.

    Anyone carrying an unsubscribe suppression — what turning BOTH streams off
    leaves behind, see ``sync_unsubscribe_suppression`` — is likewise left alone.
    They already said no, and being granted hub access is not consent to
    re-subscribe them; flipping the opt-ins here would also delete that
    suppression row on the next sync and silently undo their choice.

    Returns whether the opt-ins were applied. The caller owns the commit.
    """
    if not contact.email:
        return False

    if contact.auto_emails or contact.broadcast_emails:
        return False

    unsubscribed = (
        db.query(EmailSuppression)
        .filter(
            EmailSuppression.email == contact.email,
            EmailSuppression.reason == UNSUBSCRIBE_REASON,
        )
        .first()
    )
    if unsubscribed is not None:
        return False

    contact.auto_emails = True
    contact.broadcast_emails = True
    return True
