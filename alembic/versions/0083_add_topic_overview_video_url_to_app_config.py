"""add topic_overview_video_url to app_config

Adds the global "topic overview" video URL shown on the school topics/journey
page. Left NULL (no video configured) at launch, which hides the "Watch video
overview" button until an admin sets it.

Revision ID: 0083
Revises: 0082
Create Date: 2026-07-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: Union[str, None] = "0082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("topic_overview_video_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_config", "topic_overview_video_url")
