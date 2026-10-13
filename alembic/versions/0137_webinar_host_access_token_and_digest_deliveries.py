"""Webinar host-page token and host digest delivery ledger

``webinars.host_access_token`` is the random token behind the public co-host
questions page; NULL until a link is first issued, unique so a lookup by token
is an index hit.

``webinar_host_digest_deliveries`` records each (webinar, recipient, start time)
the pre-workshop host digest was claimed for. The unique key is the atomic
claim; a reschedule changes the start time and so earns a fresh email.

Revision ID: 0137
Revises: 0136
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webinars", sa.Column("host_access_token", sa.String(64), nullable=True))
    op.create_unique_constraint("webinars_host_access_token_key", "webinars", ["host_access_token"])
    op.create_table(
        "webinar_host_digest_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "webinar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webinars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("start_datetime", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "webinar_id", "recipient_email", "start_datetime", name="uq_webinar_host_digest_delivery"
        ),
    )


def downgrade() -> None:
    op.drop_table("webinar_host_digest_deliveries")
    op.drop_constraint("webinars_host_access_token_key", "webinars", type_="unique")
    op.drop_column("webinars", "host_access_token")
