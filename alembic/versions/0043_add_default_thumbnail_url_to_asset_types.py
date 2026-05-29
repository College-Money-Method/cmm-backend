"""add_default_thumbnail_url_to_asset_types

@42
@41
Create Date: 2026-05-14 10:14:33.100518
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0043'
down_revision: Union[str, None] = '0042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("default_thumbnail_url", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "default_thumbnail_url")
