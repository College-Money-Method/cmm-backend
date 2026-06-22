"""Admin-set default send dates for communication templates, per cycle.

These serve as suggested defaults for all schools in the cycle. Schools can
override them by saving their own CommunicationScheduleItem.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class CommunicationTemplateDefaultDate(Base):
    __tablename__ = "communication_template_default_dates"
    __table_args__ = (
        UniqueConstraint("template_id", "cycle_id", name="uq_template_default_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("communication_templates.id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False
    )
    suggested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    template: Mapped["CommunicationTemplate"] = relationship(  # noqa: F821
        "CommunicationTemplate", foreign_keys=[template_id], lazy="noload"
    )
