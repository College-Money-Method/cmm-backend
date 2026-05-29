"""merge branches

@20
@12, p4q5r6s7t8u9, r6s7t8u9v0w1
Create Date: 2026-04-13 10:59:38.262710
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0020'
down_revision: Union[str, None] = ('0012', '0017', '0019')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
