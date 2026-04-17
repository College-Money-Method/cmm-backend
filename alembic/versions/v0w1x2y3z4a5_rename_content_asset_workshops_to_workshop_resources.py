"""rename content_asset_workshops to workshop_resources

Revision ID: v0w1x2y3z4a5
Revises: u9v0w1x2y3z4
Create Date: 2026-04-17

"""
from __future__ import annotations

from alembic import op

revision = "v0w1x2y3z4a5"
down_revision = "u9v0w1x2y3z4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("content_asset_workshops", "workshop_resources")


def downgrade() -> None:
    op.rename_table("workshop_resources", "content_asset_workshops")
