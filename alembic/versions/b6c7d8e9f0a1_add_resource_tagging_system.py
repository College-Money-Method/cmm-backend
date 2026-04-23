"""add resource tagging system

- New entities: resource_categories, milestones
- Join tables for category→topic/workshop, milestone→grade_config/goal/topic/workshop
- content_assets: popularity_score, click_count
- asset_types: is_public, display_bucket

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "b6c7d8e9f0a1"
down_revision = "a5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── content_assets: popularity + clicks ─────────────────────────────────
    op.add_column(
        "content_assets",
        sa.Column("popularity_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("click_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ── asset_types: is_public + display_bucket ─────────────────────────────
    op.add_column(
        "asset_types",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "asset_types",
        sa.Column("display_bucket", sa.Text(), nullable=True),
    )

    # ── resource_categories ─────────────────────────────────────────────────
    op.create_table(
        "resource_categories",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_resource_categories_name"),
        sa.UniqueConstraint("slug", name="uq_resource_categories_slug"),
    )
    op.create_index("idx_resource_categories_sort_order", "resource_categories", ["sort_order"])

    op.create_table(
        "resource_category_topics",
        sa.Column("resource_category_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("resource_category_id", "topic_id"),
        sa.ForeignKeyConstraint(
            ["resource_category_id"], ["resource_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_resource_category_topics_topic_id", "resource_category_topics", ["topic_id"])

    op.create_table(
        "resource_category_workshops",
        sa.Column("resource_category_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("resource_category_id", "workshop_id"),
        sa.ForeignKeyConstraint(
            ["resource_category_id"], ["resource_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_resource_category_workshops_workshop_id",
        "resource_category_workshops",
        ["workshop_id"],
    )

    # ── milestones ──────────────────────────────────────────────────────────
    op.create_table(
        "milestones",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_milestones_name"),
        sa.UniqueConstraint("slug", name="uq_milestones_slug"),
    )
    op.create_index("idx_milestones_sort_order", "milestones", ["sort_order"])

    op.create_table(
        "milestone_grade_configs",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("grade_config_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("milestone_id", "grade_config_id"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grade_config_id"], ["grade_configs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_milestone_grade_configs_grade_config_id",
        "milestone_grade_configs",
        ["grade_config_id"],
    )

    op.create_table(
        "milestone_goals",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("milestone_id", "goal_id"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_milestone_goals_goal_id", "milestone_goals", ["goal_id"])

    op.create_table(
        "milestone_topics",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("milestone_id", "topic_id"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_milestone_topics_topic_id", "milestone_topics", ["topic_id"])

    op.create_table(
        "milestone_workshops",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("milestone_id", "workshop_id"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_milestone_workshops_workshop_id", "milestone_workshops", ["workshop_id"])


def downgrade() -> None:
    # Milestones and joins
    op.drop_index("idx_milestone_workshops_workshop_id", table_name="milestone_workshops")
    op.drop_table("milestone_workshops")

    op.drop_index("idx_milestone_topics_topic_id", table_name="milestone_topics")
    op.drop_table("milestone_topics")

    op.drop_index("idx_milestone_goals_goal_id", table_name="milestone_goals")
    op.drop_table("milestone_goals")

    op.drop_index("idx_milestone_grade_configs_grade_config_id", table_name="milestone_grade_configs")
    op.drop_table("milestone_grade_configs")

    op.drop_index("idx_milestones_sort_order", table_name="milestones")
    op.drop_table("milestones")

    # Resource categories and joins
    op.drop_index(
        "idx_resource_category_workshops_workshop_id",
        table_name="resource_category_workshops",
    )
    op.drop_table("resource_category_workshops")

    op.drop_index(
        "idx_resource_category_topics_topic_id",
        table_name="resource_category_topics",
    )
    op.drop_table("resource_category_topics")

    op.drop_index("idx_resource_categories_sort_order", table_name="resource_categories")
    op.drop_table("resource_categories")

    # asset_types columns
    op.drop_column("asset_types", "display_bucket")
    op.drop_column("asset_types", "is_public")

    # content_assets columns
    op.drop_column("content_assets", "click_count")
    op.drop_column("content_assets", "popularity_score")
