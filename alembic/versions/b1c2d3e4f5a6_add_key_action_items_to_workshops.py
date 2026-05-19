"""add key_action_items to workshops

Revision ID: b1c2d3e4f5a6
Revises: 5eaeec6a6b5a
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b1c2d3e4f5a6"
down_revision = "5eaeec6a6b5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workshops",
        sa.Column("key_action_items", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("workshops", "key_action_items")
