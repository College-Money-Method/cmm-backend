"""drop milestone tables and milestone_label from grade_configs

@40
@38
Create Date: 2026-04-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop join tables before the parent milestones table
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
    op.drop_column("grade_configs", "milestone_label")


def downgrade() -> None:
    op.add_column("grade_configs", sa.Column("milestone_label", sa.Text(), nullable=True))
    op.create_table(
        "milestones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_milestones_sort_order", "milestones", ["sort_order"])
    op.create_table(
        "milestone_grade_configs",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("grade_config_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grade_config_id"], ["grade_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id", "grade_config_id"),
    )
    op.create_index("idx_milestone_grade_configs_grade_config_id", "milestone_grade_configs", ["grade_config_id"])
    op.create_table(
        "milestone_goals",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id", "goal_id"),
    )
    op.create_index("idx_milestone_goals_goal_id", "milestone_goals", ["goal_id"])
    op.create_table(
        "milestone_topics",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id", "topic_id"),
    )
    op.create_index("idx_milestone_topics_topic_id", "milestone_topics", ["topic_id"])
    op.create_table(
        "milestone_workshops",
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id", "workshop_id"),
    )
    op.create_index("idx_milestone_workshops_workshop_id", "milestone_workshops", ["workshop_id"])
