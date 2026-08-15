"""Open/click aggregate reporting for sent emails (Phase 7 analytics fast-follow).

Counts DISTINCT ``send_log_id`` per event type, never raw ``email_event`` rows:
Apple Mail Privacy Protection re-fetches the open pixel repeatedly, so a single
recipient can produce many open events — unique-recipient counts are the only
meaningful denominator. Open rates are inherently inflated by MPP; the UI states
this caveat explicitly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.emails.models import EmailEvent, EmailSendLog


@dataclass
class EmailEngagement:
    """Aggregate engagement for a set of sent emails."""

    sent_count: int
    unique_opened: int
    unique_clicked: int

    @property
    def open_rate(self) -> float:
        """Fraction of sent emails with >=1 open event (0.0 when nothing sent)."""
        return self.unique_opened / self.sent_count if self.sent_count else 0.0

    @property
    def click_rate(self) -> float:
        """Fraction of sent emails with >=1 click event (0.0 when nothing sent)."""
        return self.unique_clicked / self.sent_count if self.sent_count else 0.0


def _unique_with_event(db: Session, base_send_filter, event_type: str) -> int:
    """Count distinct send-log rows (matching ``base_send_filter``) that have at
    least one ``email_event`` of ``event_type``."""
    stmt = (
        select(func.count(func.distinct(EmailEvent.send_log_id)))
        .join(EmailSendLog, EmailSendLog.id == EmailEvent.send_log_id)
        .where(base_send_filter, EmailEvent.event_type == event_type)
    )
    return db.scalar(stmt) or 0


def engagement_for_broadcast(db: Session, broadcast_id: uuid.UUID) -> EmailEngagement:
    """Open/click engagement for one broadcast's live (``status="sent"``) rows."""
    base = (EmailSendLog.broadcast_id == broadcast_id) & (EmailSendLog.status == "sent")
    sent_count = db.scalar(select(func.count()).select_from(EmailSendLog).where(base)) or 0
    return EmailEngagement(
        sent_count=sent_count,
        unique_opened=_unique_with_event(db, base, "open"),
        unique_clicked=_unique_with_event(db, base, "click"),
    )


def engagement_for_source(db: Session, source: str) -> EmailEngagement:
    """Open/click engagement across all live sends of a given ``source``
    (e.g. ``"pre_workshop"``)."""
    base = (EmailSendLog.source == source) & (EmailSendLog.status == "sent")
    sent_count = db.scalar(select(func.count()).select_from(EmailSendLog).where(base)) or 0
    return EmailEngagement(
        sent_count=sent_count,
        unique_opened=_unique_with_event(db, base, "open"),
        unique_clicked=_unique_with_event(db, base, "click"),
    )
