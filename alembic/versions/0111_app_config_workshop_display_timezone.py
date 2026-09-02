"""app-wide default display timezone for workshop dates in emails

Adds ``app_config.workshop_display_timezone``: the zone workshop
``{{date}}``/``{{time}}`` merge tags render in for any school that has no
``display_timezone`` of its own.

Nullable with no default on purpose — NULL means "use the env seed
(``settings.workshop_display_timezone``)", so an existing deployment keeps
rendering exactly as it did until an admin sets a value in Global Settings.

Revision ID: 0111
Revises: 0110
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: Union[str, None] = "0110"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_config", sa.Column("workshop_display_timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_config", "workshop_display_timezone")
