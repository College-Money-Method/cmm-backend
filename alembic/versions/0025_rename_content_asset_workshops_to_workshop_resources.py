"""rename content_asset_workshops to workshop_resources

@25
@24
Create Date: 2026-04-17

"""
from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("content_asset_workshops", "workshop_resources")


def downgrade() -> None:
    op.rename_table("workshop_resources", "content_asset_workshops")
