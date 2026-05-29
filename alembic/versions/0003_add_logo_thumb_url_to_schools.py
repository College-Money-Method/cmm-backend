"""add logo_thumb_url to schools

@03
@02
Create Date: 2026-03-19 10:32:42.733020
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schools', sa.Column('logo_thumb_url', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('schools', 'logo_thumb_url')
