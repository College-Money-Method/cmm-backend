"""SQLAlchemy model for per-school, per-cycle communication schedule items."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class CommunicationScheduleItem(Base):
    __tablename__ = "communication_schedule_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False
    )
    # 'announcement' | 'followup' | 'communication'
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    webinar_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("communication_templates.id", ondelete="SET NULL"), nullable=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # False once the counselor manually overrides the auto-computed default
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Eagerly loaded in all schedule queries via selectinload
    template: Mapped["CommunicationTemplate | None"] = relationship(  # noqa: F821
        "CommunicationTemplate", foreign_keys=[template_id], lazy="noload"
    )
