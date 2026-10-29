"""SQLAlchemy models for the College Aid Explorer.

``colleges`` is identity (one row per institution, uuid key). Aid facts live in
``college_aid_records``, one row per (college, CDS year), so each school
versions independently and the tool shows each school's latest available year.
Only source facts are stored; derived percentages are computed at read time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base

# The eight CMM regions; kept here so the CHECK and the import validator share it.
REGIONS = (
    "Great Lakes", "Mid-Atlantic", "New England", "Plains",
    "Rocky Mts", "Southeast", "Southwest", "West Coast",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class College(Base):
    __tablename__ = "colleges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # display name; latest import wins
    name_key: Mapped[str] = mapped_column(Text, nullable=False)  # normalize_name(name), import matching
    state: Mapped[str] = mapped_column(Text, nullable=False)  # USPS code
    region: Mapped[str] = mapped_column(Text, nullable=False)
    control: Mapped[str] = mapped_column(Text, nullable=False)  # 'public' | 'private'
    hidden_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # hidden, history kept
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    records: Mapped[list[CollegeAidRecord]] = relationship(
        back_populates="college", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("name_key", "state", name="uq_colleges_name_key_state"),
        CheckConstraint("length(state) = 2", name="ck_colleges_state"),
        CheckConstraint(_in_list("control", ("public", "private")), name="ck_colleges_control"),
        CheckConstraint(_in_list("region", REGIONS), name="ck_colleges_region"),
    )


class CollegeAidRecord(Base):
    __tablename__ = "college_aid_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    college_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("colleges.id", ondelete="CASCADE"), nullable=False
    )
    # 'YYYY-YY'. NULL = the source did not report a year (at most one such row per college).
    cds_year: Mapped[str | None] = mapped_column(Text)
    coa_in_state: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # publics only
    coa_out_of_state: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # privates: the COA
    coa_year: Mapped[str | None] = mapped_column(Text)  # COA year can differ from cds_year
    coa_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    full_time_undergrads: Mapped[int | None] = mapped_column(Integer)
    aid_application: Mapped[str | None] = mapped_column(Text)  # raw text, normalized at read
    # Fractions 0..1. NULL = N/R, never 0.
    pct_need_met: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    pct_non_need_receiving_merit: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    avg_merit_award: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    source: Mapped[str | None] = mapped_column(Text)
    release_label: Mapped[str] = mapped_column(Text, nullable=False)  # 'YYYY-MM' of the last writing release
    data_notes: Mapped[str | None] = mapped_column(Text)
    # Set by the admin edit form; an import warns before overwriting such a row.
    manually_edited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    manually_edited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # admin user id
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    college: Mapped[College] = relationship(back_populates="records")

    __table_args__ = (
        UniqueConstraint("college_id", "cds_year", name="uq_college_aid_records_college_year"),
        # A plain UNIQUE treats NULLs as distinct, so the N/R-year row needs its own guard.
        Index(
            "uq_college_aid_records_college_no_year",
            "college_id",
            unique=True,
            postgresql_where=text("cds_year IS NULL"),
            sqlite_where=text("cds_year IS NULL"),
        ),
        CheckConstraint("cds_year IS NULL OR length(cds_year) = 7", name="ck_college_aid_records_cds_year"),
        CheckConstraint("length(release_label) = 7", name="ck_college_aid_records_release_label"),
        CheckConstraint(
            "pct_need_met IS NULL OR pct_need_met BETWEEN 0 AND 1", name="ck_college_aid_records_pct_need_met"
        ),
        CheckConstraint(
            "pct_non_need_receiving_merit IS NULL OR pct_non_need_receiving_merit BETWEEN 0 AND 1",
            name="ck_college_aid_records_pct_merit",
        ),
        CheckConstraint(
            "(coa_in_state IS NULL OR coa_in_state >= 0) AND (coa_out_of_state IS NULL OR coa_out_of_state >= 0)"
            " AND (avg_merit_award IS NULL OR avg_merit_award >= 0)"
            " AND (full_time_undergrads IS NULL OR full_time_undergrads >= 0)",
            name="ck_college_aid_records_non_negative",
        ),
    )
