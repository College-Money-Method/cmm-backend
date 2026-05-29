"""add school_override to portal_mapping

@30
@29
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portal_mapping",
        sa.Column("school_override", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("portal_mapping", "school_override")
