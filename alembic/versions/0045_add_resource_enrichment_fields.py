"""add_resource_enrichment_fields

@44
@43
Create Date: 2026-05-16 11:47:26.149178
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0045'
down_revision: Union[str, None] = '0044'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("content_assets", sa.Column("why_important", sa.Text(), nullable=True))
    op.add_column("content_assets", sa.Column("how_to_use", sa.Text(), nullable=True))
    op.add_column("content_assets", sa.Column("suggested_grades", sa.Text(), nullable=True))
    op.add_column("content_assets", sa.Column("tags", sa.ARRAY(sa.Text()), nullable=True, server_default="{}"))


def downgrade() -> None:
    op.drop_column("content_assets", "tags")
    op.drop_column("content_assets", "suggested_grades")
    op.drop_column("content_assets", "how_to_use")
    op.drop_column("content_assets", "why_important")
