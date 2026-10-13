"""Allow the webinar host digest as an email_send_log source

The host digest goes through ``emails.ses_client.send_email``, which logs every
send with the caller's ``source``; ``ck_email_send_log_source`` must list it or
every digest send would fail on the log insert (same shape as revision 0136).
Downgrade re-narrows the constraint, so host digest log rows are deleted first.

Revision ID: 0138
Revises: 0137
"""

from __future__ import annotations

from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

_SOURCES_BEFORE = "'broadcast', 'pre_workshop', 'followup', 'post_workshop', 'video_pipeline', 'survey_digest'"


def _set_email_sources(sources: str) -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint("ck_email_send_log_source", "email_send_log", f"source IN ({sources})")


def upgrade() -> None:
    _set_email_sources(f"{_SOURCES_BEFORE}, 'webinar_host_digest'")


def downgrade() -> None:
    op.execute("DELETE FROM email_send_log WHERE source = 'webinar_host_digest'")
    _set_email_sources(_SOURCES_BEFORE)
