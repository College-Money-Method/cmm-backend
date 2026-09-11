"""Operator-supplied transcript URL on a video job

A pasted download URL brings no captions with it, so a manual run had no
transcript and fell back to silence-detection trimming and frame-only
chaptering. This column records the transcript an operator supplies alongside
the source, and doubles as an override for a Zoom recording whose account had
transcription switched off.

Revision ID: 0124
Revises: 0123
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webinar_video_jobs", sa.Column("transcript_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "transcript_url")
