"""Enable pg_trgm extension and add trigram indexes for fuzzy content search.

@08
@07
Create Date: 2026-03-26
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_assets_name_trgm "
        "ON content_assets USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_assets_description_trgm "
        "ON content_assets USING gin (description gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_content_assets_description_trgm")
    op.execute("DROP INDEX IF EXISTS idx_content_assets_name_trgm")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
