"""add airtable_sync_logs table

Revision ID: c6d7e8f9a0b1
Revises: b7c8d9e0f1a2
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "airtable_sync_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synced_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("matched", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_airtable_sync_logs_synced_at", "airtable_sync_logs", ["synced_at"])


def downgrade() -> None:
    op.drop_index("idx_airtable_sync_logs_synced_at", table_name="airtable_sync_logs")
    op.drop_table("airtable_sync_logs")
