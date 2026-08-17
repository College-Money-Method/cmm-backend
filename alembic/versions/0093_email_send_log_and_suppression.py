"""email send log and suppression — outbound email tracking + bounce/complaint suppression

Backs SES-based automation email sending: email_send_log records every
attempted send (sent/dry_run/suppressed/failed); email_suppression blocks
future sends to addresses that bounced, complained, or unsubscribed.

Revision ID: 0093
Revises: 0092
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093"
down_revision: Union[str, None] = "0092"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_send_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("rendered_html", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('sent', 'dry_run', 'suppressed', 'failed')",
            name="ck_email_send_log_status",
        ),
        sa.CheckConstraint(
            "source IN ('broadcast', 'pre_workshop', 'followup')",
            name="ck_email_send_log_source",
        ),
    )
    op.create_index("idx_email_send_log_recipient_email", "email_send_log", ["recipient_email"])
    op.create_index("idx_email_send_log_provider_message_id", "email_send_log", ["provider_message_id"])

    op.create_table(
        "email_suppression",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('bounce', 'complaint', 'unsubscribe')",
            name="ck_email_suppression_reason",
        ),
        sa.UniqueConstraint("email", name="uq_email_suppression_email"),
    )


def downgrade() -> None:
    op.drop_table("email_suppression")
    op.drop_index("idx_email_send_log_provider_message_id", table_name="email_send_log")
    op.drop_index("idx_email_send_log_recipient_email", table_name="email_send_log")
    op.drop_table("email_send_log")
