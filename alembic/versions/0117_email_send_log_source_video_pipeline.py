"""email_send_log source — allow 'video_pipeline'

The video pipeline runs unattended, so a failed job has no operator watching it.
Its only push channel is an ops email, and that email goes through the same
``emails.ses_client.send_email`` path as everything else — which writes an
``email_send_log`` row stamped with the caller's ``source``.

``ck_email_send_log_source`` enumerates the allowed values, so without this
revision the alert send raises an IntegrityError at the point where something
has *already* gone wrong. Widening the CHECK is the whole change.

Downgrade re-narrows the constraint, so any video_pipeline rows must be cleared
first — the same shape as revision 0100, which added ``post_workshop``.

Revision ID: 0117
Revises: 0116
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0117"
down_revision: Union[str, None] = "0116"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_source",
        "email_send_log",
        "source IN ('broadcast', 'pre_workshop', 'followup', 'post_workshop', 'video_pipeline')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_source",
        "email_send_log",
        "source IN ('broadcast', 'pre_workshop', 'followup', 'post_workshop')",
    )
