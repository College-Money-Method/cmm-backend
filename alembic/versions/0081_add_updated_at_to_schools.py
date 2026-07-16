"""schools: add updated_at (drives list-version cache invalidation)

Adds ``updated_at`` (nullable, ORM ``onupdate=now``) so the frontend can cheaply
detect when the school list changed (create/edit/delete) and refetch its cached
list only then. Existing rows are backfilled to ``created_at`` so the initial
version is stable.

Revision ID: 0081
Revises: 0080
Create Date: 2026-07-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: Union[str, None] = "0080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schools",
        sa.Column("updated_at", sa.dialects.postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    # Backfill so max(updated_at|created_at) is well-defined from day one.
    op.execute("UPDATE schools SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    op.drop_column("schools", "updated_at")
