"""Translated-caption progress for a published replay.

Captions are made after the video is live, not before it. Vimeo writes the
English transcript itself and has no webhook to announce it, so the pipeline has
to keep looking — and holding a finished replay back for hours while it looks
would delay the thing schools are waiting for.

That work therefore cannot live in ``state``: `published` is terminal, and it is
terminal on purpose, because it is the one state that means a school's page has
a working player on it. These columns track the caption follow-up beside it,
where a caption failure can be seen and retried without reopening a published
job.

``captions_state`` holds `pending` while the English track is still being looked
for, `running` while translation is under way, then `completed`, `failed`, or
`skipped` — the last meaning Vimeo never produced a transcript to translate,
which is an outcome rather than a fault. ``captions_attempts`` counts the sweeps
that looked and found nothing, and is what eventually ends the search.

Replays published before this column existed are backfilled to `skipped`: they
were never in scope, and starting the whole back catalogue translating on the
next sweep is not what adding a column should do.

Revision ID: 0122
Revises: 0121
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0122"
down_revision: Union[str, None] = "0121"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webinar_video_jobs",
        sa.Column(
            "captions_state",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "webinar_video_jobs",
        sa.Column("captions_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("webinar_video_jobs", sa.Column("captions_error", sa.Text(), nullable=True))
    op.add_column(
        "webinar_video_jobs",
        sa.Column("captions_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # Rows that already exist were published before captions were part of the
    # pipeline. Leaving them `pending` would send the next sweep off to
    # translate the whole back catalogue at once; they are marked as never
    # having been in scope instead.
    op.execute("UPDATE webinar_video_jobs SET captions_state = 'skipped'")


def downgrade() -> None:
    op.drop_column("webinar_video_jobs", "captions_completed_at")
    op.drop_column("webinar_video_jobs", "captions_error")
    op.drop_column("webinar_video_jobs", "captions_attempts")
    op.drop_column("webinar_video_jobs", "captions_state")
