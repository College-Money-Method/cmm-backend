"""add content_asset_states and content_asset_schools visibility join tables

Adds two additional visibility dimensions for content assets alongside cohorts:
- content_asset_states: restrict an asset to schools in a 2-letter state code
- content_asset_schools: restrict an asset to specific schools

Visibility is additive (OR) across cohort/state/school. No rows in any
dimension => asset is visible to everyone.

Revision ID: 0088
Revises: 0087
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0088"
down_revision: Union[str, None] = "0087"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_asset_states",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "state"),
    )
    op.create_table(
        "content_asset_schools",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["school_id"], ["schools.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "school_id"),
    )


def downgrade() -> None:
    op.drop_table("content_asset_schools")
    op.drop_table("content_asset_states")
