"""SQLAlchemy models for cycles and cohorts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Date, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Cycle(Base):
    __tablename__ = "cycles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    beginning_date: Mapped[datetime | None] = mapped_column(Date)
    end_date: Mapped[datetime | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    next_cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cycles.id"))
    prev_cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cycles.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    webinars: Mapped[list[Webinar]] = relationship(back_populates="cycle")
    sales: Mapped[list[Sale]] = relationship(back_populates="cycle")
    assets: Mapped[list[Asset]] = relationship(back_populates="cycle")
    one_on_one_meetings: Mapped[list[OneOnOneMeeting]] = relationship(back_populates="cycle")

    __table_args__ = (
        Index("idx_cycles_one_current", "is_current", unique=True, postgresql_where=(is_current == True)),  # noqa: E712
    )


class Cohort(Base):
    __tablename__ = "cohorts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hide_unavailability_calendar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    schools: Mapped[list[School]] = relationship(back_populates="cohort")
    webinars: Mapped[list[Webinar]] = relationship(back_populates="cohort")
    content_assets = relationship("ContentAsset", secondary="content_asset_cohorts", back_populates="cohorts")
