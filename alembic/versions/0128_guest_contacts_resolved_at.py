"""Reply tracking for contact-form submissions

Admins work the inbox by hand and had no way to tell an answered enquiry from an
untouched one, so a reread of the list was the only way to know what still owed
a reply. ``resolved_at`` records the moment it was answered; null means it is
still outstanding.

A timestamp rather than a boolean: the flag is derivable from it, and the date
is what the screen shows.

Revision ID: 0128
Revises: 0127
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guest_contacts",
        sa.Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guest_contacts", "resolved_at")
