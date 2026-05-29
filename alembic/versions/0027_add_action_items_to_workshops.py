"""add action_items to workshops

@27
@26
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workshops",
        sa.Column("action_items", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("workshops", "action_items")
