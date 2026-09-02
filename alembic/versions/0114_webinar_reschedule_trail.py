"""webinar reschedule trail

Adds ``webinars.previous_start_datetime`` and ``webinars.rescheduled_at``: where a
session used to start, and when it was moved.

Written only when the admin PATCH moves ``start_datetime`` materially into the
future (see ``_validate_webinar_schedule`` in src/workshops/router.py). A
historical date correction, an Airtable backfill and a Zoom-derived value all
leave both columns alone, because none of those means the announced session
moved. That distinction is what decides whether the workshop automations are
re-armed and every mapped counselor is emailed again, so it has to be recorded
rather than re-derived later.

NULL on every existing row: no reschedule has ever been tracked before now.

Revision ID: 0114
Revises: 0113
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0114"
down_revision: Union[str, None] = "0113"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webinars",
        sa.Column("previous_start_datetime", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "webinars",
        sa.Column("rescheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webinars", "rescheduled_at")
    op.drop_column("webinars", "previous_start_datetime")
