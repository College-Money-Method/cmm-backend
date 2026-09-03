"""SQLAlchemy models for schools, contacts, and school date selectors."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Computed, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base

if TYPE_CHECKING:
    from src.auth.models import UserRole


class School(Base):
    __tablename__ = "schools"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    street_address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(Text)
    # Explicit override for the zone this school's workshop emails are written
    # in. NULL = derive it from `state` (see src/schools/display_timezone.py).
    # Set it for a school on the wrong side of a zone boundary within its state
    # — Chattanooga and Knoxville are Eastern while Tennessee maps to Central.
    display_timezone: Mapped[str | None] = mapped_column(Text)
    enrollment_9_12: Mapped[int | None] = mapped_column(Integer)
    # Self-reported by counselors in the Hub; enrollment_9_12 is kept in sync
    # as the total whenever any per-grade value is updated (see update_school)
    enrollment_grade_9: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_10: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_11: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_12: Mapped[int | None] = mapped_column(Integer)
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
    # Raw slug value sourced from Airtable; slug field is owned by the application
    airtable_slug: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[str | None] = mapped_column(Text)

    @property
    def has_password(self) -> bool:
        return bool(self.cmm_website_password)

    school_resource_center_url: Mapped[str | None] = mapped_column(Text)
    appointlet_link: Mapped[str | None] = mapped_column(Text)
    calendar_link: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    logo_thumb_url: Mapped[str | None] = mapped_column(Text)
    is_current_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # SRC (School Resource Center) is auto-enabled for current customers. This
    # flag lets an admin activate the SRC for a prospect so they can be given a
    # preview link before becoming a customer. Effective public access is
    # is_current_customer OR is_cmm_website_activated (see _find_public_school).
    is_cmm_website_activated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    cohort_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cohorts.id"))
    grade_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("grade_sets.id", ondelete="SET NULL"), nullable=True
    )
    bubble_rec_id: Mapped[str | None] = mapped_column(Text)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    # Bumped on any ORM update (manual edit, Airtable sync). Drives the cheap
    # list-version signal the frontend uses to invalidate its cached school list.
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, onupdate=func.now()
    )

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


class SchoolEnrollmentCycle(Base):
    """Per-cycle self-reported enrollment history for a school.

    Holds enrollment for NON-current cycles only. The current cycle's values
    live directly on ``schools.enrollment_grade_9..12`` (source of truth for the
    ``enrollment_9_12`` total and the ``enrollment_range`` computed column that
    powers Hub analytics). See ``update_school`` / the enrollment-cycles router.
    """

    __tablename__ = "school_enrollment_cycles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False
    )
    enrollment_grade_9: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_10: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_11: Mapped[int | None] = mapped_column(Integer)
    enrollment_grade_12: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("school_id", "cycle_id", name="uq_school_enrollment_cycles_school_cycle"),
        Index("idx_school_enrollment_cycles_school_id", "school_id"),
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    # Nullable — a counselor contact may not be attached to a school yet
    school_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schools.id", ondelete="SET NULL"), nullable=True)
    # Supabase auth user provisioned for this contact (auth.users.id, no cross-schema FK)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True, index=True)
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
    # Two independent opt-ins, both self-managed by the counselor on the Hub Team
    # page: `auto_emails` governs scheduler-driven workshop automations,
    # `broadcast_emails` governs one-off admin broadcasts. Neither implies the other.
    auto_emails: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    broadcast_emails: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    softr_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Hub display preference: the IANA zone this counselor reads workshop times
    # on. NULL = their browser's own zone. Screen-only — emails are rendered in
    # the app-wide zone for everyone (see src/schools/display_timezone.py).
    timezone: Mapped[str | None] = mapped_column(Text)
    # Set when a contact is removed entirely from Airtable (soft-deactivation).
    # Row is kept so the provisioned auth account can be reconciled/revoked.
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    @property
    def is_active(self) -> bool:
        """A contact is active (not deactivated) while ``deleted_at`` is unset."""
        return self.deleted_at is None

    school: Mapped[School | None] = relationship(back_populates="contacts")

    # The hub role provisioned for this contact. Joined on user_id rather than a
    # real FK because user_id points at Supabase's auth.users (no cross-schema
    # FK). Read-only: sync_provisioning owns writes to user_roles.
    user_role: Mapped["UserRole | None"] = relationship(
        "UserRole",
        primaryjoin="foreign(UserRole.user_id) == Contact.user_id",
        viewonly=True,
        uselist=False,
    )

    @property
    def hub_role(self) -> str | None:
        """Hub permission (``hub_admin``/``hub_user``) or None when no login.

        Surfaced on school detail so an admin can see the column the hub
        actually scopes a session by, instead of inferring it from the
        Airtable-owned `role` label.
        """
        return self.user_role.role if self.user_role else None

    __table_args__ = (
        Index("idx_contacts_school_id", "school_id"),
    )


class SchoolDateSelector(Base):
    __tablename__ = "school_date_selector"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    school_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    workshop_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("workshops.id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School] = relationship(back_populates="date_selectors")
    workshop: Mapped[Workshop | None] = relationship(back_populates="date_selectors")
