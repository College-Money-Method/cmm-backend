"""broadcast multi-school/cohort targeting, per-send sender, grouped sends, broadcast opt-in

Four related changes:

1. ``contacts.broadcast_emails`` — the second, independent opt-in surfaced on the
   Counselor Hub Team page. ``auto_emails`` keeps governing scheduler-driven
   workshop automations; this one governs broadcasts. Backfilled from
   ``auto_emails`` so nobody's effective subscription changes on deploy.
2. ``broadcast.school_ids`` / ``broadcast.cohort_ids`` replace the single-valued
   ``school_scope``. Both are JSON-encoded arrays stored as Text (matching the
   ``body_json`` convention — portable across Postgres prod and the SQLite test
   DB). Both empty = every customer school. Existing rows migrate to a
   1-element ``school_ids`` (or empty for the old "all_customers").
3. ``broadcast.sender_name`` / ``sender_email`` (also on ``email_automation``) —
   the From identity chosen per send; NULL falls back to ``settings.ses_from_email``.
4. ``broadcast.group_by_school`` — when true the send fans out one email per
   school with every recipient of that school in To.

Revision ID: 0104
Revises: 0103
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0104"
down_revision: Union[str, None] = "0103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Second opt-in on contacts ──────────────────────────────────────────
    op.add_column(
        "contacts",
        sa.Column("broadcast_emails", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute("UPDATE contacts SET broadcast_emails = auto_emails")

    # ── 2. Multi-school / cohort targeting ────────────────────────────────────
    op.add_column("broadcast", sa.Column("school_ids", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("broadcast", sa.Column("cohort_ids", sa.Text(), nullable=False, server_default="[]"))
    # "all_customers" -> [] (no school restriction); a stored school_id -> one-element array.
    op.execute(
        """
        UPDATE broadcast
        SET school_ids = CASE
            WHEN school_scope = 'all_customers' THEN '[]'
            ELSE '["' || school_scope || '"]'
        END
        """
    )
    op.drop_column("broadcast", "school_scope")

    # ── 3. Per-send sender identity ───────────────────────────────────────────
    op.add_column("broadcast", sa.Column("sender_name", sa.Text(), nullable=True))
    op.add_column("broadcast", sa.Column("sender_email", sa.Text(), nullable=True))
    op.add_column("email_automation", sa.Column("sender_name", sa.Text(), nullable=True))
    op.add_column("email_automation", sa.Column("sender_email", sa.Text(), nullable=True))

    # ── 4. Grouped (one email per school) sends ───────────────────────────────
    op.add_column(
        "broadcast",
        sa.Column("group_by_school", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("broadcast", "group_by_school")
    op.drop_column("email_automation", "sender_email")
    op.drop_column("email_automation", "sender_name")
    op.drop_column("broadcast", "sender_email")
    op.drop_column("broadcast", "sender_name")

    op.add_column(
        "broadcast", sa.Column("school_scope", sa.Text(), nullable=False, server_default="all_customers")
    )
    # Collapse back to the single-valued scope: the first targeted school, or
    # "all_customers" when the broadcast targeted every customer school. A
    # multi-school broadcast loses the extra targets — unavoidable going back
    # to a scalar column.
    op.execute(
        """
        UPDATE broadcast
        SET school_scope = CASE
            WHEN school_ids IS NULL OR school_ids IN ('[]', '') THEN 'all_customers'
            ELSE replace(replace(replace(school_ids, '[', ''), ']', ''), '"', '')
        END
        """
    )
    op.execute("UPDATE broadcast SET school_scope = split_part(school_scope, ',', 1)")
    op.drop_column("broadcast", "cohort_ids")
    op.drop_column("broadcast", "school_ids")

    op.drop_column("contacts", "broadcast_emails")
