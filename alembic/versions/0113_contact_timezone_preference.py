"""per-counselor Hub timezone preference

Adds ``contacts.timezone``: the IANA zone a counselor reads workshop times on in
the Counselor Hub. NULL — the default for every existing row — means "use
whatever zone this person's browser is in", which is what the Hub did before
this column existed.

Display-only, and deliberately so: outbound email is rendered in the app-wide
zone for everyone (``app_config.workshop_display_timezone``), so one person's
screen preference can never change what a family receives.

Revision ID: 0113
Revises: 0112
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0113"
down_revision: Union[str, None] = "0112"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "timezone")
