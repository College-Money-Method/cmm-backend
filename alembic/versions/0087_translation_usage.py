"""add translation_usage ledger table

One row per Bedrock translation invocation (cache miss) recording token usage
and computed USD cost, for spend tracking by locale / over time.

Revision ID: 0087
Revises: 0086
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0087"
down_revision: Union[str, None] = "0086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "translation_usage",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_translation_usage_locale", "translation_usage", ["locale"])
    op.create_index("idx_translation_usage_created_at", "translation_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_translation_usage_created_at", table_name="translation_usage")
    op.drop_index("idx_translation_usage_locale", table_name="translation_usage")
    op.drop_table("translation_usage")
