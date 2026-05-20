"""Add workshop_notification_subscribers table.

Revision ID: aa1bb2cc3dd4
Revises: b1c2d3e4f5a6
Create Date: 2026-05-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "aa1bb2cc3dd4"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workshop_notification_subscribers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("school_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_name", sa.Text(), nullable=True),
        sa.Column("subscribed_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("notification_types", JSONB(), server_default=sa.text("""'["registration_open"]'"""), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", "school_id", "cycle_name", name="uq_workshop_notif_email_school_cycle"),
    )
    op.create_index("idx_workshop_notif_email", "workshop_notification_subscribers", ["email"])
    op.create_index("idx_workshop_notif_school_id", "workshop_notification_subscribers", ["school_id"])
    op.create_index("idx_workshop_notif_cycle_name", "workshop_notification_subscribers", ["cycle_name"])
    op.create_index("idx_workshop_notif_subscribed_at", "workshop_notification_subscribers", ["subscribed_at"])


def downgrade() -> None:
    op.drop_index("idx_workshop_notif_subscribed_at", table_name="workshop_notification_subscribers")
    op.drop_index("idx_workshop_notif_cycle_name", table_name="workshop_notification_subscribers")
    op.drop_index("idx_workshop_notif_school_id", table_name="workshop_notification_subscribers")
    op.drop_index("idx_workshop_notif_email", table_name="workshop_notification_subscribers")
    op.drop_table("workshop_notification_subscribers")
