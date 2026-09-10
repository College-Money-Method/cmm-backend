"""Poster frame for a workshop's published replays.

Vimeo picks its own frame from the video when nothing is set, which for these
recordings is whatever the presenter's screen happened to show at that instant.
A workshop can now carry an image to use instead.

It lives on the workshop rather than on the webinar or the job because it is a
property of the workshop's branding, not of one sitting of it. The pipeline
reads it at upload time and never re-applies it, so replacing the image changes
the sessions recorded afterwards and leaves the ones already published alone.

Nullable with no default: a workshop without one uploads no thumbnail at all,
which is the behaviour every existing workshop had before this column.

Revision ID: 0121
Revises: 0120
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0121"
down_revision: Union[str, None] = "0120"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workshops", sa.Column("recording_thumbnail_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workshops", "recording_thumbnail_url")
