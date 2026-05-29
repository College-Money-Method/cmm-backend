"""add is_tool to asset_types, rename display_bucket calculator→tools

- asset_types: add is_tool BOOLEAN NOT NULL DEFAULT false
- asset_types: rename display_bucket value 'calculator' → 'tools' in existing data

@34
@32
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("is_tool", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Migrate existing 'calculator' bucket values to 'tools'
    op.execute(
        "UPDATE asset_types SET display_bucket = 'tools' WHERE display_bucket = 'calculator'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE asset_types SET display_bucket = 'calculator' WHERE display_bucket = 'tools'"
    )
    op.drop_column("asset_types", "is_tool")
