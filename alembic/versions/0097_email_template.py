"""email template — CMM-branded reusable email templates

Adds `email_template`: super-admin-managed templates selectable (one-time
prefill) from Broadcasts (category="broadcast") and Automations
(category="workshop_automation"). Distinct from counselor-facing
CommunicationTemplate / WorkshopEmailTemplate.

Revision ID: 0097
Revises: 0096
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0097"
down_revision: Union[str, None] = "0096"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_template",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('broadcast', 'workshop_automation')", name="ck_email_template_category"
        ),
    )
    op.create_index("idx_email_template_category", "email_template", ["category"])


def downgrade() -> None:
    op.drop_index("idx_email_template_category", table_name="email_template")
    op.drop_table("email_template")
