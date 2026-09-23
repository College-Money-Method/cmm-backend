"""Per-run wait counter for recordings Zoom has not finished processing

The wait for Zoom's files was bounded by ``attempt``, which retry also bumps. A
job that had already waited out its budget once therefore failed on the first
"still processing" after a retry, so re-arming it could never succeed.
``source_waits`` counts only the current run's waits and resets on retry.

Revision ID: 0129
Revises: 0128
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webinar_video_jobs",
        sa.Column("source_waits", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "source_waits")
