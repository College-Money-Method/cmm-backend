"""College Aid Explorer: colleges, per-year aid records, tool password columns

``colleges`` is identity (uuid key; UNIQUE (name_key, state) blocks duplicates
from blank-id import rows). ``college_aid_records`` holds one row per (college,
CDS year); a NULL year means "not reported", and the partial unique index keeps
that to one row per college (a plain UNIQUE treats NULLs as distinct).
``app_config`` gains the shared tool password hash (scrypt, never plaintext).

Revision ID: 0141
Revises: 0140
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_REGIONS = (
    "'Great Lakes', 'Mid-Atlantic', 'New England', 'Plains', "
    "'Rocky Mts', 'Southeast', 'Southwest', 'West Coast'"
)


def upgrade() -> None:
    op.create_table(
        "colleges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("control", sa.Text(), nullable=False),
        sa.Column("hidden_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("name_key", "state", name="uq_colleges_name_key_state"),
        sa.CheckConstraint("length(state) = 2", name="ck_colleges_state"),
        sa.CheckConstraint("control IN ('public', 'private')", name="ck_colleges_control"),
        sa.CheckConstraint(f"region IN ({_REGIONS})", name="ck_colleges_region"),
    )

    op.create_table(
        "college_aid_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "college_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("colleges.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cds_year", sa.Text(), nullable=True),
        sa.Column("coa_in_state", sa.Numeric(10, 2), nullable=True),
        sa.Column("coa_out_of_state", sa.Numeric(10, 2), nullable=True),
        sa.Column("coa_year", sa.Text(), nullable=True),
        sa.Column("coa_estimated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("full_time_undergrads", sa.Integer(), nullable=True),
        sa.Column("aid_application", sa.Text(), nullable=True),
        sa.Column("pct_need_met", sa.Numeric(5, 4), nullable=True),
        sa.Column("pct_non_need_receiving_merit", sa.Numeric(5, 4), nullable=True),
        sa.Column("avg_merit_award", sa.Numeric(10, 2), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("release_label", sa.Text(), nullable=False),
        sa.Column("data_notes", sa.Text(), nullable=True),
        sa.Column("manually_edited_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("manually_edited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("college_id", "cds_year", name="uq_college_aid_records_college_year"),
        sa.CheckConstraint("cds_year IS NULL OR length(cds_year) = 7", name="ck_college_aid_records_cds_year"),
        sa.CheckConstraint("length(release_label) = 7", name="ck_college_aid_records_release_label"),
        sa.CheckConstraint(
            "pct_need_met IS NULL OR pct_need_met BETWEEN 0 AND 1", name="ck_college_aid_records_pct_need_met"
        ),
        sa.CheckConstraint(
            "pct_non_need_receiving_merit IS NULL OR pct_non_need_receiving_merit BETWEEN 0 AND 1",
            name="ck_college_aid_records_pct_merit",
        ),
        sa.CheckConstraint(
            "(coa_in_state IS NULL OR coa_in_state >= 0) AND (coa_out_of_state IS NULL OR coa_out_of_state >= 0)"
            " AND (avg_merit_award IS NULL OR avg_merit_award >= 0)"
            " AND (full_time_undergrads IS NULL OR full_time_undergrads >= 0)",
            name="ck_college_aid_records_non_negative",
        ),
    )
    op.create_index(
        "uq_college_aid_records_college_no_year",
        "college_aid_records",
        ["college_id"],
        unique=True,
        postgresql_where=sa.text("cds_year IS NULL"),
    )

    op.add_column("app_config", sa.Column("college_aid_password_hash", sa.Text(), nullable=True))
    op.add_column(
        "app_config",
        sa.Column("college_aid_password_updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_config", "college_aid_password_updated_at")
    op.drop_column("app_config", "college_aid_password_hash")
    op.drop_index("uq_college_aid_records_college_no_year", table_name="college_aid_records")
    op.drop_table("college_aid_records")
    op.drop_table("colleges")
