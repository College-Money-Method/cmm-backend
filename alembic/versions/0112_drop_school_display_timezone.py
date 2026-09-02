"""drop the per-school workshop display timezone

Removes ``schools.display_timezone``. The zone workshop ``{{date}}``/``{{time}}``
merge tags render in is now a single app-wide setting
(``app_config.workshop_display_timezone``, revision 0111): every workshop runs
on the same US schedule, and the rendered time carries its abbreviation
("7:00 PM EDT"), so one zone reads unambiguously in any state.

Per-person display preferences live on ``contacts.timezone`` (revision 0113) and
affect only the Hub screen, never an email.

Downgrade re-adds the column as nullable; the per-school values it held are not
recoverable, which is intended — the resolution layer that read them is gone.

Revision ID: 0112
Revises: 0111
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0112"
down_revision: Union[str, None] = "0111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("schools", "display_timezone")


def downgrade() -> None:
    op.add_column("schools", sa.Column("display_timezone", sa.Text(), nullable=True))
