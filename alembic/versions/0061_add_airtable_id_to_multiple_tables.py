"""add airtable_id to cycles, contacts, portal_mapping, sales, one_on_one_meetings, workshop_registrations, school_date_selector

@61
@60
Create Date: 2026-06-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = [
    "cycles",
    "contacts",
    "portal_mapping",
    "sales",
    "one_on_one_meetings",
    "workshop_registrations",
    "school_date_selector",
]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("airtable_id", sa.Text(), nullable=True))
        op.create_unique_constraint(f"uq_{table}_airtable_id", table, ["airtable_id"])
        op.create_index(f"ix_{table}_airtable_id", table, ["airtable_id"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_airtable_id", table_name=table)
        op.drop_constraint(f"uq_{table}_airtable_id", table, type_="unique")
        op.drop_column(table, "airtable_id")
