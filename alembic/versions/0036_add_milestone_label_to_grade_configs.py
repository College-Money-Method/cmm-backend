"""add milestone_label to grade_configs

@36
@34
Create Date: 2026-04-23

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("grade_configs", sa.Column("milestone_label", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("grade_configs", "milestone_label")
