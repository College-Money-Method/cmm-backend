"""add analytics_query_cache table

Durable cache for PostHog API responses. Survives process restarts and allows
stale-on-error serving when PostHog is unavailable.

@75
@74
Create Date: 2026-07-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: Union[str, None] = "0074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analytics_query_cache",
        sa.Column("key", sa.Text(), nullable=False, primary_key=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("analytics_query_cache")
