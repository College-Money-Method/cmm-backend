"""Paul booking: the review-email inbox moves from env to Global Settings

``app_config.paul_booking_notify_email`` is nullable; null means Paul's own
address, which is what the env default sent to before.

Revision ID: 0140
Revises: 0139
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_config", sa.Column("paul_booking_notify_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_config", "paul_booking_notify_email")
