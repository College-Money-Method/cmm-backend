"""add key_action_items to workshops

@47
@46
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workshops",
        sa.Column("key_action_items", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("workshops", "key_action_items")
