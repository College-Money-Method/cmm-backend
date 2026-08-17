"""email_send_log automation_id — link sends back to their EmailAutomation

Adds a nullable `automation_id` FK (mirrors `broadcast_id`) so automation
sends can be counted per-automation (`sent_count` in the Automations admin
tab), and extends the `source` check constraint to allow `post_workshop`
(the new post-workshop-reminder automation type's log source).

Revision ID: 0100
Revises: 0099
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0100"
down_revision: Union[str, None] = "0099"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_send_log", sa.Column("automation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_email_send_log_automation_id",
        "email_send_log",
        "email_automation",
        ["automation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_email_send_log_automation_id", "email_send_log", ["automation_id"])

    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_source",
        "email_send_log",
        "source IN ('broadcast', 'pre_workshop', 'followup', 'post_workshop')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_email_send_log_source", "email_send_log", type_="check")
    op.create_check_constraint(
        "ck_email_send_log_source",
        "email_send_log",
        "source IN ('broadcast', 'pre_workshop', 'followup')",
    )

    op.drop_index("idx_email_send_log_automation_id", table_name="email_send_log")
    op.drop_constraint("fk_email_send_log_automation_id", "email_send_log", type_="foreignkey")
    op.drop_column("email_send_log", "automation_id")
