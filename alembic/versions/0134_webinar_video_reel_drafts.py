"""Trailer reels start as drafts an admin reviews before rendering

A reel used to go from one prompt straight to a render, so every refinement
cost a Fargate render. Now the clip selection is stored on the row
(``selection``) with the conversation that shaped it (``messages``), and the
render only starts once an admin approves it. Orientation is picked at that
point, so it is null while the reel is a draft.

Revision ID: 0134
Revises: 0133
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webinar_video_reels",
                  sa.Column("selection", postgresql.JSONB(), nullable=True))
    op.add_column("webinar_video_reels",
                  sa.Column("messages", postgresql.JSONB(), nullable=True))
    op.alter_column("webinar_video_reels", "orientation", nullable=True)


def downgrade() -> None:
    # Drafts have no orientation and nothing to go back to without the columns.
    op.execute("DELETE FROM webinar_video_reels WHERE orientation IS NULL")
    op.alter_column("webinar_video_reels", "orientation", nullable=False)
    op.drop_column("webinar_video_reels", "messages")
    op.drop_column("webinar_video_reels", "selection")
