"""Quarantine flag for bot submissions of the public contact form

The form is open and uncaptcha'd, and roughly one in six rows is a form-stuffing
bot or a cold SEO pitch. Submissions are classified on the way in rather than
rejected, so a misfiring rule costs the admin a look in the Spam tab instead of
a lost enquiry. ``spam_reason`` records which rule matched.

The partial index serves the inbox query, which is always
``WHERE is_spam = false ORDER BY created_at DESC``.

Revision ID: 0127
Revises: 0126
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guest_contacts",
        sa.Column("is_spam", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("guest_contacts", sa.Column("spam_reason", sa.Text(), nullable=True))
    op.create_index(
        "idx_guest_contacts_inbox",
        "guest_contacts",
        ["created_at"],
        postgresql_where=sa.text("is_spam = false"),
    )


def downgrade() -> None:
    op.drop_index("idx_guest_contacts_inbox", table_name="guest_contacts")
    op.drop_column("guest_contacts", "spam_reason")
    op.drop_column("guest_contacts", "is_spam")
