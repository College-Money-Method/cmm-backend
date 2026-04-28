"""add airtable_id to webinars

Revision ID: b7c8d9e0f1a2
Revises: d9f0a1b2c3e4
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "d9f0a1b2c3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webinars", sa.Column("airtable_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_webinars_airtable_id", "webinars", ["airtable_id"])
    op.create_index("idx_webinars_airtable_id", "webinars", ["airtable_id"])


def downgrade() -> None:
    op.drop_index("idx_webinars_airtable_id", table_name="webinars")
    op.drop_constraint("uq_webinars_airtable_id", "webinars", type_="unique")
    op.drop_column("webinars", "airtable_id")
