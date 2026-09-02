"""per-school display timezone for workshop dates in emails

Adds ``schools.display_timezone``: the IANA zone a school's workshop
``{{date}}``/``{{time}}`` merge tags are rendered in.

Nullable with no default on purpose — NULL means "use the app-wide
``settings.workshop_display_timezone``", which is a live setting, not a value
worth freezing into every existing row at migration time.

Revision ID: 0110
Revises: 0109
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0110"
down_revision: Union[str, None] = "0109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("display_timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("schools", "display_timezone")
