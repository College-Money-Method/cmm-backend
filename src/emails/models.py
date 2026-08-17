"""SQLAlchemy models for outbound email logging and bounce/complaint suppression."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class EmailSendLog(Base):
    """One row per attempted send: sent, dry-run (dev), suppressed, or failed."""

    __tablename__ = "email_send_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recipient_email: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # SES MessageId — only set for status="sent". Lets the SNS bounce/complaint
    # webhook correlate an event back to the send that triggered it.
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Populated only for dry-run rows — dev never calls SES, so this is the only
    # way to inspect what would have been sent. Real "sent" rows leave this null
    # (avoid storing PII-adjacent content indefinitely for live sends).
    rendered_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set only when source="broadcast" (Phase 3) — links a send row back to the
    # Broadcast it was sent as part of. Nullable so pre_workshop/followup rows
    # (no Broadcast) and any row logged before this column existed stay valid.
    broadcast_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("broadcast.id", ondelete="SET NULL"), nullable=True
    )
    # Set only when source="pre_workshop"/"post_workshop" — links a send row
    # back to the EmailAutomation it was sent as part of. Nullable so
    # broadcast/followup rows (no automation) stay valid.
    automation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("email_automation.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('sent', 'dry_run', 'suppressed', 'failed', 'sandboxed')",
            name="ck_email_send_log_status",
        ),
        CheckConstraint(
            "source IN ('broadcast', 'pre_workshop', 'followup', 'post_workshop')",
            name="ck_email_send_log_source",
        ),
        Index("idx_email_send_log_recipient_email", "recipient_email"),
        Index("idx_email_send_log_provider_message_id", "provider_message_id"),
        Index("idx_email_send_log_broadcast_id", "broadcast_id"),
        Index("idx_email_send_log_automation_id", "automation_id"),
    )


class EmailEvent(Base):
    """Open/click tracking event tied to one EmailSendLog row (Phase 7 analytics).

    Populated only by ``webhook_router.py``'s SES Open/Click branches, never by
    application code directly. A single send can produce many rows (e.g. Apple
    Mail Privacy Protection re-fetching the open pixel) — aggregate reporting in
    ``analytics.py`` counts distinct ``send_log_id``s, not raw event rows.
    """

    __tablename__ = "email_event"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    send_log_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("email_send_log.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Set only for event_type="click" — the URL the recipient followed.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_type IN ('open', 'click')", name="ck_email_event_type"),
        Index("idx_email_event_send_log_id", "send_log_id"),
    )


class EmailSuppression(Base):
    """Recipient addresses excluded from future sends (bounce/complaint/unsubscribe).

    Checked unconditionally by ``ses_client.send_email`` before any other branch —
    no caller can bypass it.
    """

    __tablename__ = "email_suppression"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "reason IN ('bounce', 'complaint', 'unsubscribe')",
            name="ck_email_suppression_reason",
        ),
    )
