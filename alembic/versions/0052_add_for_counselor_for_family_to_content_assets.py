"""add for_counselor and for_family to content_assets

@52
@51
Create Date: 2026-05-30

"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_assets", sa.Column("for_counselor", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("content_assets", sa.Column("for_family", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("content_assets", "for_family")
    op.drop_column("content_assets", "for_counselor")
