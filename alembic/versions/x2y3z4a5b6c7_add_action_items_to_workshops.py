"""add action_items to workshops

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "x2y3z4a5b6c7"
down_revision = "w1x2y3z4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workshops",
        sa.Column("action_items", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("workshops", "action_items")
