"""add airtable_id to schools and cohorts

@53
@52
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("airtable_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_schools_airtable_id", "schools", ["airtable_id"])
    op.create_index("idx_schools_airtable_id", "schools", ["airtable_id"])

    op.add_column("cohorts", sa.Column("airtable_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_cohorts_airtable_id", "cohorts", ["airtable_id"])
    op.create_index("idx_cohorts_airtable_id", "cohorts", ["airtable_id"])


def downgrade() -> None:
    op.drop_index("idx_cohorts_airtable_id", table_name="cohorts")
    op.drop_constraint("uq_cohorts_airtable_id", "cohorts", type_="unique")
    op.drop_column("cohorts", "airtable_id")

    op.drop_index("idx_schools_airtable_id", table_name="schools")
    op.drop_constraint("uq_schools_airtable_id", "schools", type_="unique")
    op.drop_column("schools", "airtable_id")
