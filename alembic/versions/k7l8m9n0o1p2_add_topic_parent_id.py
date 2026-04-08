"""Add parent_id to topics for sub-topic hierarchy.

Revision ID: k7l8m9n0o1p2
Revises: j6k7l8m9n0o1
Create Date: 2026-04-08
"""

from alembic import op
import sqlalchemy as sa


revision = "k7l8m9n0o1p2"
down_revision = "j6k7l8m9n0o1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_topics_parent_id", "topics", "topics",
        ["parent_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("idx_topics_parent_id", "topics", ["parent_id"])


def downgrade() -> None:
    op.drop_index("idx_topics_parent_id", table_name="topics")
    op.drop_constraint("fk_topics_parent_id", "topics", type_="foreignkey")
    op.drop_column("topics", "parent_id")
