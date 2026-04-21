"""add school_override to portal_mapping

Revision ID: a5b6c7d8e9f0
Revises: z4a5b6c7d8e9
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a5b6c7d8e9f0"
down_revision = "z4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portal_mapping",
        sa.Column("school_override", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("portal_mapping", "school_override")
