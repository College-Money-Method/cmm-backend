"""add resource_category_assets join table

Lets a content asset be assigned directly to a resource category, without
needing an intermediate Topic or Workshop. Category -> asset resolution is a
union of three sources: direct assignments (this table), category topics, and
category workshops.

Revision ID: 0090
Revises: 0089
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: Union[str, None] = "0089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_category_assets",
        sa.Column("resource_category_id", sa.Uuid(), nullable=False),
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_category_id"], ["resource_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("resource_category_id", "content_asset_id"),
    )
    # Reverse lookup: "which categories is this asset in?" (asset detail page)
    op.create_index(
        "idx_resource_category_assets_asset_id",
        "resource_category_assets",
        ["content_asset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_resource_category_assets_asset_id", table_name="resource_category_assets"
    )
    op.drop_table("resource_category_assets")
