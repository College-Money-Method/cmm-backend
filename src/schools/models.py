"""SQLAlchemy models for schools, contacts, and school date selectors."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Computed, Date, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class School(Base):
    __tablename__ = "schools"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    street_address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(Text)
    enrollment_9_12: Mapped[int | None] = mapped_column(Integer)
    enrollment_range: Mapped[str | None] = mapped_column(
        Text,
        Computed(
            "CASE "
            "WHEN enrollment_9_12 IS NULL THEN NULL "
            "WHEN enrollment_9_12 < 250 THEN '< 250' "
            "WHEN enrollment_9_12 <= 500 THEN '250-500' "
            "ELSE '>500' "
            "END"
        ),
    )
    cmm_website_password: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    school_resource_center_url: Mapped[str | None] = mapped_column(Text)
    appointlet_link: Mapped[str | None] = mapped_column(Text)
    calendar_link: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    logo_thumb_url: Mapped[str | None] = mapped_column(Text)
    is_current_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    cohort_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cohorts.id"))
    grade_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("grade_sets.id", ondelete="SET NULL"), nullable=True
    )
    bubble_rec_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    cohort: Mapped[Cohort | None] = relationship(back_populates="schools")
    grade_set: Mapped[GradeSet | None] = relationship("GradeSet")
    contacts: Mapped[list[Contact]] = relationship(back_populates="school")
    sales: Mapped[list[Sale]] = relationship(back_populates="school")
    workshop_registrations: Mapped[list[WorkshopRegistration]] = relationship(back_populates="school")
    portal_mappings: Mapped[list[PortalMapping]] = relationship(back_populates="school")
    one_on_one_meetings: Mapped[list[OneOnOneMeeting]] = relationship(back_populates="school")
    date_selectors: Mapped[list[SchoolDateSelector]] = relationship(back_populates="school")
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole",
        primaryjoin="School.id == foreign(UserRole.school_id)",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_schools_cohort_id", "cohort_id"),
        Index("idx_schools_slug", "slug"),
        Index("idx_schools_grade_set_id", "grade_set_id"),
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(
        Text,
        Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"),
    )
    email: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    magic_link: Mapped[str | None] = mapped_column(Text)
    receive_comms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    auto_emails: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    softr_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School] = relationship(back_populates="contacts")

    __table_args__ = (
        Index("idx_contacts_school_id", "school_id"),
    )


class SchoolDateSelector(Base):
    __tablename__ = "school_date_selector"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    workshop_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("workshops.id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School] = relationship(back_populates="date_selectors")
    workshop: Mapped[Workshop | None] = relationship(back_populates="date_selectors")
