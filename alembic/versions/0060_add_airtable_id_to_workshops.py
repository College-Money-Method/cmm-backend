"""add airtable_id to workshops

@60
@59
Create Date: 2026-06-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workshops", sa.Column("airtable_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_workshops_airtable_id", "workshops", ["airtable_id"])
    op.create_index("ix_workshops_airtable_id", "workshops", ["airtable_id"])


def downgrade() -> None:
    op.drop_index("ix_workshops_airtable_id", table_name="workshops")
    op.drop_constraint("uq_workshops_airtable_id", "workshops", type_="unique")
    op.drop_column("workshops", "airtable_id")
