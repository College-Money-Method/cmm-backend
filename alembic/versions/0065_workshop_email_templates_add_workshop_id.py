"""add workshop_id to workshop_email_templates

@65
@64
Create Date: 2026-06-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: Union[str, None] = "0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable so existing global templates are preserved; new templates are workshop-scoped
    op.add_column(
        "workshop_email_templates",
        sa.Column("workshop_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workshop_email_templates_workshop",
        "workshop_email_templates",
        "workshops",
        ["workshop_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_workshop_email_templates_workshop",
        "workshop_email_templates",
        ["workshop_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_workshop_email_templates_workshop", table_name="workshop_email_templates")
    op.drop_constraint("fk_workshop_email_templates_workshop", "workshop_email_templates", type_="foreignkey")
    op.drop_column("workshop_email_templates", "workshop_id")
