"""add read_time_minutes and video_duration_seconds to topics

@41
@39, f3a4b5c6d7e8
Create Date: 2026-05-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = ("0039", "0040")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("read_time_minutes", sa.Integer, nullable=True))
    op.add_column("topics", sa.Column("video_duration_seconds", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("topics", "video_duration_seconds")
    op.drop_column("topics", "read_time_minutes")
