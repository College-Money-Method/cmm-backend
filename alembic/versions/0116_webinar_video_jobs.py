"""webinar_video_jobs — durable state for the Zoom -> Vimeo replay pipeline

One row per Zoom cloud recording, carrying the pipeline from ``pending`` through
``processing`` (ECS task: download, archive, trim, sample, upload) and
``chaptering`` (API: Bedrock vision, chapter write-back) to ``published``.

The row exists because the pipeline is routinely mid-flight during an API
deploy. The pre-existing in-process job store (src/content/video_cc_jobs.py) is
the wrong model for that: a restart loses it. Postgres survives the restart, so
a job can be resumed, inspected from the admin screen, and retried.

``zoom_recording_uuid`` is UNIQUE, and that constraint — not handler logic — is
what makes the pipeline idempotent. Zoom retries webhooks, and the hourly
reconcile sweep deliberately re-offers recordings it has already seen; both
paths insert and let the constraint reject the duplicate. A plain index would
not do: it would make the lookup fast and still allow the second row.

``failed_notified_at`` records that the ops alert for the *current* failure has
gone out. Retry clears it, so a job that fails twice notifies twice, while a
sweeper that reads the same failed row every five minutes notifies once.

``ON DELETE CASCADE`` on webinar_id: a job is meaningless without its webinar,
and the webinar delete guard already refuses to remove a webinar with live
dependents for the cases that matter.

Revision ID: 0116
Revises: 0115
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0116"
down_revision: Union[str, None] = "0115"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webinar_video_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "webinar_id",
            sa.Uuid(),
            sa.ForeignKey("webinars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Idempotency key. UNIQUE is load-bearing — see module docstring.
        sa.Column("zoom_recording_uuid", sa.Text(), nullable=False, unique=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        # Written at dispatch so container logs can be correlated with the job.
        sa.Column("ecs_task_arn", sa.Text(), nullable=True),
        # Seconds of dead opening removed; fractional, hence Numeric not Integer.
        sa.Column("trim_offset_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("source_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("vimeo_video_id", sa.Text(), nullable=True),
        # Vimeo issues a privacy hash for some privacy modes; embed-only videos
        # may not have one, so this stays nullable rather than being derived.
        sa.Column("vimeo_hash", sa.Text(), nullable=True),
        sa.Column("chapters", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("failed_notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # state: read on every dispatch to count in-flight tasks against the cap.
    op.create_index("idx_webinar_video_jobs_state", "webinar_video_jobs", ["state"])
    op.create_index("idx_webinar_video_jobs_webinar_id", "webinar_video_jobs", ["webinar_id"])


def downgrade() -> None:
    op.drop_index("idx_webinar_video_jobs_webinar_id", table_name="webinar_video_jobs")
    op.drop_index("idx_webinar_video_jobs_state", table_name="webinar_video_jobs")
    op.drop_table("webinar_video_jobs")
