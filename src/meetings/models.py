"""SQLAlchemy model for one-on-one meetings."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Computed, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class OneOnOneMeeting(Base):
    __tablename__ = "one_on_one_meetings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    school_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schools.id"))
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cycles.id"))
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(
        Text,
        Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"),
    )
    email: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    meeting_goals: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    college_list: Mapped[str | None] = mapped_column(Text)
    conference_url: Mapped[str | None] = mapped_column(Text)
    is_school_sponsored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_invoiced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    ai_meeting_summary: Mapped[str | None] = mapped_column(Text)
    reminder_1_sent_on: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    reminder_2_sent_on: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School | None] = relationship(back_populates="one_on_one_meetings")
    cycle: Mapped[Cycle | None] = relationship(back_populates="one_on_one_meetings")

    __table_args__ = (
        Index("idx_1on1_school_id", "school_id"),
        Index("idx_1on1_cycle_id", "cycle_id"),
    )
