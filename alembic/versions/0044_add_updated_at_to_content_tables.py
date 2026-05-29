"""add_updated_at_to_content_tables

@43
@42
Create Date: 2026-05-14 11:48:03.824019
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0044'
down_revision: Union[str, None] = '0043'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ["asset_types", "goals", "topics", "objectives", "faqs", "grade_sets", "grade_configs"]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "updated_at")
