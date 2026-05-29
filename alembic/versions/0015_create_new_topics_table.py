"""Create new content-rich topics table and join tables.

@15
@14
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topics",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("action_items", JSONB(), nullable=False, server_default="[]"),
        sa.Column("video_embed_code", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("goals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_topics_slug", "topics", ["slug"])
    op.create_index("idx_topics_status", "topics", ["status"])
    op.create_index("idx_topics_goal_id", "topics", ["goal_id"])

    op.create_table(
        "topic_resources",
        sa.Column("topic_id", sa.Uuid(), sa.ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content_asset_id", sa.Uuid(), sa.ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "topic_faqs",
        sa.Column("topic_id", sa.Uuid(), sa.ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("faq_id", sa.Uuid(), sa.ForeignKey("faqs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("topic_faqs")
    op.drop_table("topic_resources")
    op.drop_index("idx_topics_goal_id", table_name="topics")
    op.drop_index("idx_topics_status", table_name="topics")
    op.drop_index("idx_topics_slug", table_name="topics")
    op.drop_table("topics")
