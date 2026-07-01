"""add communication_templates table

@59
@58
Create Date: 2026-06-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP
from alembic import op

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "communication_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("format", sa.Text(), nullable=False, server_default="rich_text"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("google_docs_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_comm_templates_is_active", "communication_templates", ["is_active"])
    op.create_index("idx_comm_templates_sort_order", "communication_templates", ["sort_order"])


def downgrade() -> None:
    op.drop_index("idx_comm_templates_sort_order", table_name="communication_templates")
    op.drop_index("idx_comm_templates_is_active", table_name="communication_templates")
    op.drop_table("communication_templates")
