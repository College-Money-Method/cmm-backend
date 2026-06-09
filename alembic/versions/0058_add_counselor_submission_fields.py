"""add counselor submission fields to content_assets

@58
@57
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "content_assets",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default="cmm",
        ),
    )
    op.add_column(
        "content_assets",
        sa.Column("submitted_by_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column(
        "content_assets",
        sa.Column("review_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("ai_review_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("ai_review_summary", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_content_assets_submitted_by_id",
        "content_assets",
        ["submitted_by_id"],
    )
    op.create_index(
        "idx_content_assets_review_status",
        "content_assets",
        ["review_status"],
    )


def downgrade() -> None:
    op.drop_index("idx_content_assets_review_status", table_name="content_assets")
    op.drop_index("idx_content_assets_submitted_by_id", table_name="content_assets")
    op.drop_column("content_assets", "ai_review_summary")
    op.drop_column("content_assets", "ai_review_score")
    op.drop_column("content_assets", "review_notes")
    op.drop_column("content_assets", "review_status")
    op.drop_column("content_assets", "submitted_by_id")
    op.drop_column("content_assets", "source")
