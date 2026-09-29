"""Route parent enquiries out of the partner-school inbox

The contact form is meant for counsellors, schools and businesses, but families
use it too and they turned out to be the majority of what arrives. Those are
welcome enquiries, just a different conversation, and interleaving them made the
inbox hard to work through.

``is_parent`` splits them into their own tab. It is independent of ``is_spam``:
nothing is hidden, the row keeps every control the inbox has, and an admin can
move it back. ``parent_reason`` names the rule that matched, or records that a
human made the call.

Revision ID: 0133
Revises: 0132
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guest_contacts",
        sa.Column(
            "is_parent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("guest_contacts", sa.Column("parent_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("guest_contacts", "parent_reason")
    op.drop_column("guest_contacts", "is_parent")
