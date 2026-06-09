"""add workshop_email_templates table

@57
@56
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workshop_email_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_workshop_email_templates_type", "workshop_email_templates", ["type"])


def downgrade() -> None:
    op.drop_index("idx_workshop_email_templates_type", table_name="workshop_email_templates")
    op.drop_table("workshop_email_templates")
