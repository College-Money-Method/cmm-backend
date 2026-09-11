"""Step-level progress timeline on webinar_video_jobs.

``state`` records which half of the pipeline a job is in — `processing` covers
everything from the Zoom download to the Vimeo transcode wait, which is most of
the runtime and all of the ways a run gets slow. Until now that detail existed
only as CloudWatch log lines, so the admin screen could not tell a stalled
download from a transcode that is simply taking its time.

``stage_events`` is the append-only list of ``{"stage": ..., "at": ...}`` marks
the pipeline writes as it crosses each boundary. One JSONB column rather than a
``stage`` column plus timestamps: a single list cannot disagree with itself, the
current stage is its last entry, and each stage's duration is the gap to the
next one. Nullable and defaulted to the empty list so jobs that ran before this
migration read as "no timeline recorded" instead of as a job that never started.

Revision ID: 0120
Revises: 0119
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0120"
down_revision: Union[str, None] = "0119"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webinar_video_jobs",
        sa.Column(
            "stage_events",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "stage_events")
