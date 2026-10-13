"""Survey response triage status and daily-digest tracking

Admins triage feedback-widget responses together, so each response records who
marked it reviewed and when (the email is stored alongside the id so the admin
list never has to call Supabase per row). ``digest_sent_at`` is the claim stamp
for the daily admin digest: a response is emailed once, and a missed run is
caught up because selection is by ``digest_sent_at IS NULL``, not a time window.

Existing rows are stamped as already digested so the first digest does not dump
history; their ``reviewed_at`` stays NULL so the backlog stays visible in the
admin "Needs review" filter.

The digest is sent through ``emails.ses_client.send_email``, which logs every
send to ``email_send_log`` with the caller's ``source`` — so
``ck_email_send_log_source`` is widened to allow ``survey_digest`` (the same
shape as revision 0117). Downgrade re-narrows it, so any survey_digest log rows
are deleted first.

Revision ID: 0136
Revises: 0135
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

_SOURCES_BEFORE = "'broadcast', 'pre_workshop', 'followup', 'post_workshop', 'video_pipeline'"


def _set_email_sources(sources: str) -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint("ck_email_send_log_source", "email_send_log", f"source IN ({sources})")


def upgrade() -> None:
    op.add_column("survey_responses", sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("survey_responses", sa.Column("reviewed_by", sa.Text(), nullable=True))
    op.add_column("survey_responses", sa.Column("reviewed_by_email", sa.Text(), nullable=True))
    op.add_column("survey_responses", sa.Column("digest_sent_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.execute("UPDATE survey_responses SET digest_sent_at = now()")

    # Both hot queries only ever look at the un-stamped slice, so partial
    # indexes stay tiny no matter how many responses accumulate.
    op.create_index(
        "ix_survey_responses_unreviewed",
        "survey_responses",
        ["created_at"],
        postgresql_where=sa.text("reviewed_at IS NULL"),
    )
    op.create_index(
        "ix_survey_responses_undigested",
        "survey_responses",
        ["created_at"],
        postgresql_where=sa.text("digest_sent_at IS NULL"),
    )

    _set_email_sources(f"{_SOURCES_BEFORE}, 'survey_digest'")


def downgrade() -> None:
    op.execute("DELETE FROM email_send_log WHERE source = 'survey_digest'")
    _set_email_sources(_SOURCES_BEFORE)

    op.drop_index("ix_survey_responses_undigested", table_name="survey_responses")
    op.drop_index("ix_survey_responses_unreviewed", table_name="survey_responses")
    op.drop_column("survey_responses", "digest_sent_at")
    op.drop_column("survey_responses", "reviewed_by_email")
    op.drop_column("survey_responses", "reviewed_by")
    op.drop_column("survey_responses", "reviewed_at")
