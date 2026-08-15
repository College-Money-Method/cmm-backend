"""email_send_log sandboxed status — allow 'sandboxed' send-log rows

Extends the `status` check constraint to allow `sandboxed`, the status logged
when `email_sandbox_mode` is on and a recipient is outside the sandbox domain
(sent-log row written, no SES call).

Revision ID: 0101
Revises: 0100
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0101"
down_revision: Union[str, None] = "0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_email_send_log_status", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_status",
        "email_send_log",
        "status IN ('sent', 'dry_run', 'suppressed', 'failed', 'sandboxed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_email_send_log_status", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_status",
        "email_send_log",
        "status IN ('sent', 'dry_run', 'suppressed', 'failed')",
    )
