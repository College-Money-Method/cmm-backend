"""Rename topics table to goals and update all references.

@14
@13
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
# Depends on both: grade_config page fields AND topic parent_id (which creates idx_topics_parent_id)
down_revision = ("0013", "0012")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Rename topics -> goals
    op.rename_table("topics", "goals")

    # 2. Rename content_asset_topics -> content_asset_goals, column topic_id -> goal_id
    op.rename_table("content_asset_topics", "content_asset_goals")
    op.alter_column("content_asset_goals", "topic_id", new_column_name="goal_id")

    # 3. Rename grade_config_topics -> grade_config_goals, column topic_id -> goal_id
    op.rename_table("grade_config_topics", "grade_config_goals")
    op.alter_column("grade_config_goals", "topic_id", new_column_name="goal_id")

    # Update index on goals table (parent_id)
    op.drop_index("idx_topics_parent_id", table_name="goals")
    op.create_index("idx_goals_parent_id", "goals", ["parent_id"])


def downgrade() -> None:
    op.drop_index("idx_goals_parent_id", table_name="goals")
    op.create_index("idx_topics_parent_id", "goals", ["parent_id"])

    op.alter_column("grade_config_goals", "goal_id", new_column_name="topic_id")
    op.rename_table("grade_config_goals", "grade_config_topics")

    op.alter_column("content_asset_goals", "goal_id", new_column_name="topic_id")
    op.rename_table("content_asset_goals", "content_asset_topics")

    op.rename_table("goals", "topics")
