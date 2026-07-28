"""add school_enrollment_cycles for per-cycle enrollment history

Stores self-reported enrollment for NON-current cycles. The current cycle's
values remain on schools.enrollment_grade_9..12 (source of truth for the
enrollment_9_12 total + enrollment_range computed column used by analytics).

Revision ID: 0089
Revises: 0088
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: Union[str, None] = "0088"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "school_enrollment_cycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_grade_9", sa.Integer(), nullable=True),
        sa.Column("enrollment_grade_10", sa.Integer(), nullable=True),
        sa.Column("enrollment_grade_11", sa.Integer(), nullable=True),
        sa.Column("enrollment_grade_12", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id", "cycle_id", name="uq_school_enrollment_cycles_school_cycle"
        ),
    )
    op.create_index(
        "idx_school_enrollment_cycles_school_id",
        "school_enrollment_cycles",
        ["school_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_school_enrollment_cycles_school_id",
        table_name="school_enrollment_cycles",
    )
    op.drop_table("school_enrollment_cycles")
