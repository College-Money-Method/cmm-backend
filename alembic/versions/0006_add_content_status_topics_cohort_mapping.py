"""Add content status, WP tracking fields, topics table, and content_asset_cohorts.

@06
@05
Create Date: 2026-03-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New columns on content_assets ─────────────────────────────────────────
    op.add_column(
        "content_assets",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column(
        "content_assets",
        sa.Column("wp_post_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("wp_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_content_assets_status", "content_assets", ["status"])

    # ── topics ────────────────────────────────────────────────────────────────
    op.create_table(
        "topics",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("airtable_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_id", name="uq_topics_airtable_id"),
        sa.UniqueConstraint("name", name="uq_topics_name"),
    )
    op.create_index("idx_topics_airtable_id", "topics", ["airtable_id"])

    # ── content_asset_topics (M:M) ────────────────────────────────────────────
    op.create_table(
        "content_asset_topics",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"],
            ["content_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "topic_id"),
    )
    op.create_index(
        "idx_content_asset_topics_topic_id",
        "content_asset_topics",
        ["topic_id"],
    )

    # ── content_asset_cohorts (M:M — 3.5 cohort visibility) ──────────────────
    op.create_table(
        "content_asset_cohorts",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"],
            ["content_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["cohorts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "cohort_id"),
    )
    op.create_index(
        "idx_content_asset_cohorts_cohort_id",
        "content_asset_cohorts",
        ["cohort_id"],
    )


def downgrade() -> None:
    op.drop_table("content_asset_cohorts")
    op.drop_table("content_asset_topics")
    op.drop_table("topics")
    op.drop_index("idx_content_assets_status", "content_assets")
    op.drop_column("content_assets", "wp_synced_at")
    op.drop_column("content_assets", "wp_post_id")
    op.drop_column("content_assets", "status")
