"""add unmatched_participants_count to webinars

Zoom participants with no matching registration ("joined without registering")
are counted during attendance sync — powers the per-workshop attendance tile.

@74
@73
Create Date: 2026-07-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: Union[str, None] = "0073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webinars",
        sa.Column("unmatched_participants_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webinars", "unmatched_participants_count")
