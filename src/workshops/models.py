"""SQLAlchemy models for workshops, webinars, registrations, and portal mapping."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Computed,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.db.enums import RegistrationStatus


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    key_actions: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    sequence_number: Mapped[int | None] = mapped_column(Integer, unique=True)
    suggested_grades: Mapped[str | None] = mapped_column(Text)
    resource_center_slug: Mapped[str | None] = mapped_column(Text, unique=True)
    workshop_art_url: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    webinars: Mapped[list[Webinar]] = relationship(back_populates="workshop")
    assets: Mapped[list[Asset]] = relationship(secondary="workshop_assets", back_populates="workshops")
    date_selectors: Mapped[list[SchoolDateSelector]] = relationship(back_populates="workshop")
    objectives = relationship("Objective", secondary="objective_workshops", back_populates="workshops")
    content_assets = relationship(
        "ContentAsset",
        secondary="workshop_resources",
        back_populates="workshops",
        order_by="WorkshopResource.sort_order",
    )


class Webinar(Base):
    __tablename__ = "webinars"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workshop_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workshops.id"), nullable=False)
    cohort_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cohorts.id"), nullable=True)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cycles.id"), nullable=True)
    webinar_name: Mapped[str | None] = mapped_column(Text)
    zoom_webinar_id: Mapped[str | None] = mapped_column(Text, unique=True)
    start_datetime: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    end_datetime: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    duration_minutes: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "CASE "
            "WHEN start_datetime IS NOT NULL AND end_datetime IS NOT NULL "
            "THEN EXTRACT(EPOCH FROM (end_datetime - start_datetime))::INTEGER / 60 "
            "ELSE NULL "
            "END"
        ),
    )
    join_url: Mapped[str | None] = mapped_column(Text)
    start_url: Mapped[str | None] = mapped_column(Text)
    registration_url: Mapped[str | None] = mapped_column(Text)
    zoom_link: Mapped[str | None] = mapped_column(Text)
    video_embed_code: Mapped[str | None] = mapped_column(Text)
    audio_transcript: Mapped[str | None] = mapped_column(Text)
    track_registrations: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    workshop: Mapped[Workshop] = relationship(back_populates="webinars")
    cohort: Mapped[Cohort | None] = relationship(back_populates="webinars")
    cycle: Mapped[Cycle | None] = relationship(back_populates="webinars")
    registrations: Mapped[list[WorkshopRegistration]] = relationship(back_populates="webinar")
    portal_mappings: Mapped[list[PortalMapping]] = relationship(back_populates="webinar")

    __table_args__ = (
        Index("idx_webinars_workshop_id", "workshop_id"),
        Index("idx_webinars_cohort_id", "cohort_id"),
        Index("idx_webinars_cycle_id", "cycle_id"),
        Index("idx_webinars_start_datetime", "start_datetime"),
    )


class WorkshopRegistration(Base):
    __tablename__ = "workshop_registrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    webinar_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    school_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schools.id"))
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(
        Text,
        Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"),
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    grade: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RegistrationStatus] = mapped_column(
        Enum(RegistrationStatus, name="registration_status_enum", create_constraint=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=RegistrationStatus.APPROVED,
        server_default="approved",
    )
    attended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    join_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    leave_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    zoom_registrant_id: Mapped[str | None] = mapped_column(Text)
    questions: Mapped[str | None] = mapped_column(Text)
    registration_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    webinar: Mapped[Webinar] = relationship(back_populates="registrations")
    school: Mapped[School | None] = relationship(back_populates="workshop_registrations")

    __table_args__ = (
        Index("idx_workshop_reg_webinar_id", "webinar_id"),
        Index("idx_workshop_reg_school_id", "school_id"),
        Index("idx_workshop_reg_email", "email"),
    )


class WorkshopAsset(Base):
    __tablename__ = "workshop_assets"

    workshop_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workshops.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)


class PortalMapping(Base):
    __tablename__ = "portal_mapping"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    pre_webinar_reminder_sent_on: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    post_webinar_update_sent_on: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    show_zoom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School] = relationship(back_populates="portal_mappings")
    webinar: Mapped[Webinar] = relationship(back_populates="portal_mappings")

    __table_args__ = (
        UniqueConstraint("school_id", "webinar_id", name="uq_portal_mapping_school_webinar"),
        Index("idx_portal_mapping_school_id", "school_id"),
        Index("idx_portal_mapping_webinar_id", "webinar_id"),
    )
