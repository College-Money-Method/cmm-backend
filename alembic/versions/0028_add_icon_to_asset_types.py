"""add icon to asset_types

@28
@27
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("icon", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "icon")
