"""add communication_schedule_items table

@63
@62
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP
from alembic import op

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "communication_schedule_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        # 'announcement' | 'followup' | 'communication'
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("webinar_id", sa.Uuid(), nullable=True),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("scheduled_at", TIMESTAMP(timezone=True), nullable=False),
        # True when date was auto-computed from webinar; False when manually overridden
        sa.Column("is_auto_generated", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["webinar_id"], ["webinars.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["communication_templates.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "idx_comm_schedule_school_cycle",
        "communication_schedule_items",
        ["school_id", "cycle_id"],
    )
    op.create_index(
        "idx_comm_schedule_webinar",
        "communication_schedule_items",
        ["webinar_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_comm_schedule_webinar", table_name="communication_schedule_items")
    op.drop_index("idx_comm_schedule_school_cycle", table_name="communication_schedule_items")
    op.drop_table("communication_schedule_items")
