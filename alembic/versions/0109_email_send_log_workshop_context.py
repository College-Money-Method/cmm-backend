"""link automation send-log rows to the webinar and school they were sent for

``email_send_log`` recorded only ``automation_id``, so a sent row could not be
attributed to a workshop, a school, or a cycle — the automations admin could see
"8 emails" but never which ones. Cycle scoping everywhere else in the app goes
through ``webinars.cycle_id`` (see ``analytics/postgres_queries.py``), and
filtering by ``sent_at`` instead would misfile every reminder that fires across
a cycle boundary (a pre-workshop reminder goes out days before its webinar).

Both columns are nullable: broadcast rows have no workshop context, and rows
written before this migration only get one where it can be resolved
unambiguously (below).

Revision ID: 0109
Revises: 0108
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0109"
down_revision: Union[str, None] = "0108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_send_log",
        sa.Column("webinar_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "email_send_log",
        sa.Column("school_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_email_send_log_webinar_id",
        "email_send_log",
        "webinars",
        ["webinar_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_email_send_log_school_id",
        "email_send_log",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_email_send_log_webinar_id", "email_send_log", ["webinar_id"])
    op.create_index("idx_email_send_log_school_id", "email_send_log", ["school_id"])

    # Best-effort backfill for rows already written by the automation runner.
    # The runner sends one batch per (automation, portal_mapping), so a send row
    # can be traced back through its recipient's school to the ledger claim that
    # produced it. Only applied where exactly ONE claimed mapping matches — an
    # automation that has fired for two webinars at the same school is genuinely
    # ambiguous from the log row alone, and a wrong attribution is worse than a
    # null one.
    op.execute(
        """
        UPDATE email_send_log AS l
           SET webinar_id = m.resolved_webinar_id,
               school_id  = m.resolved_school_id
          FROM (
                SELECT c.email          AS email,
                       led.automation_id AS automation_id,
                       (ARRAY_AGG(DISTINCT pm.webinar_id))[1] AS resolved_webinar_id,
                       (ARRAY_AGG(DISTINCT pm.school_id))[1]  AS resolved_school_id
                  FROM automation_send_ledger AS led
                  JOIN portal_mapping AS pm ON pm.id = led.portal_mapping_id
                  JOIN contacts AS c ON c.school_id = pm.school_id
                 WHERE c.email IS NOT NULL
                 GROUP BY c.email, led.automation_id
                HAVING COUNT(DISTINCT pm.webinar_id) = 1
                   AND COUNT(DISTINCT pm.school_id) = 1
               ) AS m
         WHERE l.automation_id = m.automation_id
           AND l.recipient_email = m.email
           AND l.webinar_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("idx_email_send_log_school_id", table_name="email_send_log")
    op.drop_index("idx_email_send_log_webinar_id", table_name="email_send_log")
    op.drop_constraint("fk_email_send_log_school_id", "email_send_log", type_="foreignkey")
    op.drop_constraint("fk_email_send_log_webinar_id", "email_send_log", type_="foreignkey")
    op.drop_column("email_send_log", "school_id")
    op.drop_column("email_send_log", "webinar_id")
