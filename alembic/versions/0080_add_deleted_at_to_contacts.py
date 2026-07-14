"""schools: add deleted_at to contacts (soft-deactivation)

Airtable-driven offboarding: a contact removed entirely from the Airtable pull
is marked with ``deleted_at`` (soft-delete, row kept so its provisioned auth
account can be reconciled/revoked). Clearing a contact's ``Sch`` link is a
separate signal (``school_id`` NULL) and does NOT set ``deleted_at``.

Revision ID: 0080
Revises: 0079
Create Date: 2026-07-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: Union[str, None] = "0079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("deleted_at", sa.dialects.postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "deleted_at")
