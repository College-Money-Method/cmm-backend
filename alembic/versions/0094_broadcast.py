"""broadcast — one-off super-admin email broadcasts

Adds the broadcast table (draft/send/audience filters for one-off admin
emails) and an additive, nullable broadcast_id FK on email_send_log (Phase 1)
linking a send-log row back to the Broadcast it was sent as part of.

Revision ID: 0094
Revises: 0093
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0094"
down_revision: Union[str, None] = "0093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broadcast",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_json", sa.Text(), nullable=False),
        sa.Column("school_scope", sa.Text(), nullable=False),
        sa.Column("role_filter", sa.Text(), nullable=False, server_default="all"),
        sa.Column("opt_in_filter", sa.Text(), nullable=False, server_default="opted_in"),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.CheckConstraint("role_filter IN ('all', 'hub_admin')", name="ck_broadcast_role_filter"),
        sa.CheckConstraint("opt_in_filter IN ('opted_in', 'all')", name="ck_broadcast_opt_in_filter"),
        sa.CheckConstraint("status IN ('draft', 'sending', 'sent', 'failed')", name="ck_broadcast_status"),
    )

    op.add_column("email_send_log", sa.Column("broadcast_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_email_send_log_broadcast_id",
        "email_send_log",
        "broadcast",
        ["broadcast_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_email_send_log_broadcast_id", "email_send_log", ["broadcast_id"])


def downgrade() -> None:
    op.drop_index("idx_email_send_log_broadcast_id", table_name="email_send_log")
    op.drop_constraint("fk_email_send_log_broadcast_id", "email_send_log", type_="foreignkey")
    op.drop_column("email_send_log", "broadcast_id")
    op.drop_table("broadcast")
