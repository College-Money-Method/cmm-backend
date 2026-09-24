"""Keep each registrant's personal Zoom join link.

Zoom returns it on the add-registrant call; storing it lets the portal show the
link on the success screen and hand it back when the same parent registers
again, rather than leaving a last-minute registrant waiting on an email.
Nullable: rows registered before this, and installs without Zoom, have none.

Revision ID: 0132
Revises: 0131
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0132"
down_revision: Union[str, None] = "0131"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workshop_registrations", sa.Column("zoom_join_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workshop_registrations", "zoom_join_url")
