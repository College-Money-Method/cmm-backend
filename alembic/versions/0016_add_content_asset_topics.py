"""Add content_asset_topics join table for linking content assets to new topics.

@16
@15
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_asset_topics",
        sa.Column("content_asset_id", sa.Uuid(), sa.ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topic_id", sa.Uuid(), sa.ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("content_asset_topics")
