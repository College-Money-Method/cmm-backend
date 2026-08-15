"""automation send ledger — per-(automation, portal_mapping) idempotency

Adds `automation_send_ledger`: replaces the old single-automation
`portal_mapping.pre_webinar_reminder_sent_on` column as the idempotency record
now that multiple automations can target the same mapping independently (see
0099 for the email_automation type/offset generalization it supports).

Revision ID: 0098
Revises: 0097
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0098"
down_revision: Union[str, None] = "0097"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_send_ledger",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("portal_mapping_id", sa.Uuid(), nullable=False),
        sa.Column(
            "sent_on",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["email_automation.id"],
            name="fk_automation_send_ledger_automation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["portal_mapping_id"],
            ["portal_mapping.id"],
            name="fk_automation_send_ledger_portal_mapping_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("automation_id", "portal_mapping_id", name="uq_automation_send_ledger"),
    )
    op.create_index("idx_automation_send_ledger_automation_id", "automation_send_ledger", ["automation_id"])


def downgrade() -> None:
    op.drop_index("idx_automation_send_ledger_automation_id", table_name="automation_send_ledger")
    op.drop_table("automation_send_ledger")
