"""Request a call with Paul: settings, days off, requests and held blocks

``app_config`` gains the kill switch, Paul's timezone and weekly hours.
``paul_meeting_request_blocks.block_start`` is UNIQUE: that constraint is the
double-booking guard. ``email_send_log`` accepts source ``paul_booking`` (same
shape as revision 0138); downgrade deletes those log rows before re-narrowing.

Revision ID: 0139
Revises: 0138
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_SOURCES_BEFORE = (
    "'broadcast', 'pre_workshop', 'followup', 'post_workshop', 'video_pipeline', "
    "'survey_digest', 'webinar_host_digest'"
)


def _set_email_sources(sources: str) -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint("ck_email_send_log_source", "email_send_log", f"source IN ({sources})")


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("paul_booking_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("app_config", sa.Column("paul_booking_timezone", sa.Text(), nullable=True))
    op.add_column("app_config", sa.Column("paul_booking_weekly_hours", postgresql.JSONB(), nullable=True))

    op.create_table(
        "paul_time_off",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("ends_on >= starts_on", name="ck_paul_time_off_range"),
    )
    op.create_index("idx_paul_time_off_range", "paul_time_off", ["starts_on", "ends_on"])

    op.create_table(
        "paul_meeting_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guest_contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guest_contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("visitor_timezone", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("claimed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decline_note", sa.Text(), nullable=True),
        sa.Column("zoom_meeting_id", sa.Text(), nullable=True),
        sa.Column("zoom_join_url", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("duration_minutes IN (30, 60)", name="ck_paul_meeting_requests_duration"),
        sa.CheckConstraint(
            "status IN ('pending', 'approving', 'approved', 'declined')",
            name="ck_paul_meeting_requests_status",
        ),
    )
    op.create_index("idx_paul_meeting_requests_start_at", "paul_meeting_requests", ["start_at"])
    op.create_index("idx_paul_meeting_requests_guest_contact_id", "paul_meeting_requests", ["guest_contact_id"])

    op.create_table(
        "paul_meeting_request_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paul_meeting_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("block_start", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("block_start", name="uq_paul_meeting_request_blocks_block_start"),
    )
    op.create_index(
        "idx_paul_meeting_request_blocks_request_id", "paul_meeting_request_blocks", ["request_id"]
    )

    _set_email_sources(f"{_SOURCES_BEFORE}, 'paul_booking'")


def downgrade() -> None:
    op.execute("DELETE FROM email_send_log WHERE source = 'paul_booking'")
    _set_email_sources(_SOURCES_BEFORE)

    op.drop_table("paul_meeting_request_blocks")
    op.drop_table("paul_meeting_requests")
    op.drop_table("paul_time_off")

    op.drop_column("app_config", "paul_booking_weekly_hours")
    op.drop_column("app_config", "paul_booking_timezone")
    op.drop_column("app_config", "paul_booking_enabled")
