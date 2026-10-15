"""Tables behind "request a call with Paul": days off, requests, held blocks."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.guest_contacts.models import GuestContact


class PaulTimeOff(Base):
    """Full days Paul takes no calls. Dates are in his booking timezone."""

    __tablename__ = "paul_time_off"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Inclusive: a single day off has starts_on == ends_on.
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="ck_paul_time_off_range"),
        Index("idx_paul_time_off_range", "starts_on", "ends_on"),
    )


class PaulMeetingRequest(Base):
    """One visitor's request for a call; holds its slot until decided or lapsed.

    "Lapsed" is never stored: ``status == 'pending' and expires_at <= now`` is
    derived wherever it matters, so nothing has to run on a timer.
    """

    __tablename__ = "paul_meeting_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    guest_contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("guest_contacts.id", ondelete="CASCADE"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    visitor_timezone: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | approving (Zoom meeting being created) | approved | declined
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decline_note: Mapped[str | None] = mapped_column(Text)
    zoom_meeting_id: Mapped[str | None] = mapped_column(Text)
    zoom_join_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    contact: Mapped[GuestContact] = relationship(GuestContact, lazy="joined")

    __table_args__ = (
        CheckConstraint("duration_minutes IN (30, 60)", name="ck_paul_meeting_requests_duration"),
        CheckConstraint(
            "status IN ('pending', 'approving', 'approved', 'declined')",
            name="ck_paul_meeting_requests_status",
        ),
        Index("idx_paul_meeting_requests_start_at", "start_at"),
        Index("idx_paul_meeting_requests_guest_contact_id", "guest_contact_id"),
    )


class PaulMeetingRequestBlock(Base):
    """One held 30-minute block. UNIQUE ``block_start`` is the double-booking guard.

    A constraint rather than a query-then-insert check, so two concurrent
    requests for overlapping times cannot both win, on Postgres and on SQLite.
    """

    __tablename__ = "paul_meeting_request_blocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("paul_meeting_requests.id", ondelete="CASCADE"), nullable=False
    )
    block_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("block_start", name="uq_paul_meeting_request_blocks_block_start"),
        Index("idx_paul_meeting_request_blocks_request_id", "request_id"),
    )
