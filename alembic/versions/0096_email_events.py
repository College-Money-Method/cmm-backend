"""email events — open/click tracking per send (Phase 7 analytics fast-follow)

Adds `email_event`, one row per SES Open/Click notification correlated back to
an `email_send_log` row via `provider_message_id` in the SNS webhook.

Revision ID: 0096
Revises: 0095
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0096"
down_revision: Union[str, None] = "0095"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("send_log_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("event_type IN ('open', 'click')", name="ck_email_event_type"),
        sa.ForeignKeyConstraint(
            ["send_log_id"],
            ["email_send_log.id"],
            name="fk_email_event_send_log_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_email_event_send_log_id", "email_event", ["send_log_id"])


def downgrade() -> None:
    op.drop_index("idx_email_event_send_log_id", table_name="email_event")
    op.drop_table("email_event")
