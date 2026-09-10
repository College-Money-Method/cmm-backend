"""Artifact locations and provenance flags on webinar_video_jobs.

The processing task writes files to three places and the rest of the pipeline
has to find them again without guessing a path convention:

* ``archive_key`` is the S3 prefix holding the untrimmed source. It is what
  makes a job re-runnable after Zoom's copy is deleted, so it is recorded on
  the row rather than derived — a derived path that drifts silently turns a
  retryable job into an unretryable one.
* ``archive_expires_at`` is when the lifecycle rule removes that source. Past
  it, retry cannot work at all, and an enabled button that cannot possibly
  work is worse than a disabled one with a reason beside it.
* ``frames_prefix`` locates the candidate frames and the two JSON manifests
  that chaptering and the admin screen both read.

The two flags exist because a job can succeed and still be worth a human
glance: ``trim_fallback_used`` says the model's trim point was rejected and the
first cue was used instead, and ``chapters_truncated`` says the chapter list hit
the cap. Neither is a failure, so neither would ever produce an alert.

Revision ID: 0118
Revises: 0117
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: Union[str, None] = "0117"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webinar_video_jobs", sa.Column("archive_key", sa.Text(), nullable=True))
    op.add_column(
        "webinar_video_jobs",
        sa.Column("archive_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("webinar_video_jobs", sa.Column("frames_prefix", sa.Text(), nullable=True))
    op.add_column("webinar_video_jobs", sa.Column("vimeo_player_embed_url", sa.Text(), nullable=True))
    op.add_column(
        "webinar_video_jobs",
        sa.Column("trim_fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "webinar_video_jobs",
        sa.Column("chapters_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "chapters_truncated")
    op.drop_column("webinar_video_jobs", "trim_fallback_used")
    op.drop_column("webinar_video_jobs", "vimeo_player_embed_url")
    op.drop_column("webinar_video_jobs", "frames_prefix")
    op.drop_column("webinar_video_jobs", "archive_expires_at")
    op.drop_column("webinar_video_jobs", "archive_key")
