"""merge branches

Revision ID: 3b73af33a7d1
Revises: k7l8m9n0o1p2, p4q5r6s7t8u9, r6s7t8u9v0w1
Create Date: 2026-04-13 10:59:38.262710
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b73af33a7d1'
down_revision: Union[str, None] = ('k7l8m9n0o1p2', 'p4q5r6s7t8u9', 'r6s7t8u9v0w1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
