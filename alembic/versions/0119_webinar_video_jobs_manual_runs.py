"""Manual audit runs on webinar_video_jobs.

Until now a job could only exist for a Zoom recording belonging to a webinar
this app knows, because the webhook was the only way one was created. An
operator also needs to put an arbitrary recording through the pipeline to audit
what it produces, and that source may be a Zoom meeting nobody registered
through us or a plain download URL. Three changes make room for it:

* ``webinar_id`` becomes nullable. An audit run has no webinar to publish to,
  and inventing a placeholder row would put a fake session in the admin lists
  every school-facing screen reads from.
* ``source_url`` holds the download URL when the source is not Zoom. Recorded
  rather than re-derived: it is the only description of where those bytes came
  from, and the archive under ``archive_key`` is what a retry actually reads.
* ``audit_only`` marks a run that must stop after Vimeo. Such a job never
  writes ``webinars.video_embed_code`` and never deletes the Zoom recording, so
  auditing the pipeline cannot change what families see or destroy a source.

``zoom_recording_uuid`` stays NOT NULL UNIQUE. URL sources get a synthetic key
instead of a relaxed constraint — that UNIQUE index is the whole idempotency
guarantee for webhook replay, and widening it to accommodate manual runs would
trade a real invariant for a convenience.

Revision ID: 0119
Revises: 0118
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0119"
down_revision: Union[str, None] = "0118"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("webinar_video_jobs", "webinar_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("webinar_video_jobs", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column(
        "webinar_video_jobs",
        sa.Column("audit_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "audit_only")
    op.drop_column("webinar_video_jobs", "source_url")
    # A job with no webinar cannot be represented once the column is NOT NULL
    # again. These are audit runs by definition — the artefacts they produced
    # are in S3 and Vimeo, and nothing downstream reads the rows.
    op.execute("DELETE FROM webinar_video_jobs WHERE webinar_id IS NULL")
    op.alter_column("webinar_video_jobs", "webinar_id", existing_type=sa.Uuid(), nullable=False)
