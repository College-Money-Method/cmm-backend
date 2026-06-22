"""add communication_template_default_dates table

@64
@63
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP
from alembic import op

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "communication_template_default_dates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("suggested_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["communication_templates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("template_id", "cycle_id", name="uq_template_default_date"),
    )
    op.create_index("idx_template_default_dates_cycle", "communication_template_default_dates", ["cycle_id"])


def downgrade() -> None:
    op.drop_index("idx_template_default_dates_cycle", table_name="communication_template_default_dates")
    op.drop_table("communication_template_default_dates")
