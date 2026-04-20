"""merge_main_and_content_chain

Revision ID: b7fb530685c4
Revises: 8a3129898ea2, z4a5b6c7d8e9
Create Date: 2026-04-19 21:59:07.419785
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7fb530685c4'
down_revision: Union[str, None] = ('8a3129898ea2', 'z4a5b6c7d8e9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
