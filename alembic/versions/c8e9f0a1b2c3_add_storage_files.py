"""add storage_files table

Revision ID: c8e9f0a1b2c3
Revises: b7fb530685c4
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8e9f0a1b2c3"
down_revision = "b7fb530685c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("s3_url", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("extension", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "content_asset_id",
            sa.Uuid(),
            sa.ForeignKey("content_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("s3_key", name="uq_storage_files_s3_key"),
    )
    op.create_index("ix_storage_files_content_asset_id", "storage_files", ["content_asset_id"])


def downgrade() -> None:
    op.drop_index("ix_storage_files_content_asset_id", table_name="storage_files")
    op.drop_table("storage_files")
