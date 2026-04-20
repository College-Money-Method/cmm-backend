"""decouple storage_files from content_assets

Revision ID: d9f0a1b2c3e4
Revises: c8e9f0a1b2c3
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9f0a1b2c3e4"
down_revision = "c8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_storage_files_content_asset_id", table_name="storage_files")
    op.drop_constraint("storage_files_content_asset_id_fkey", "storage_files", type_="foreignkey")
    op.drop_column("storage_files", "content_asset_id")
    op.drop_column("storage_files", "uploaded_by_user_id")


def downgrade() -> None:
    op.add_column("storage_files", sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("storage_files", sa.Column("content_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "storage_files_content_asset_id_fkey",
        "storage_files",
        "content_assets",
        ["content_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_storage_files_content_asset_id", "storage_files", ["content_asset_id"])
