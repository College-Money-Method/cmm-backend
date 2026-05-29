"""Add guest_contacts table for public contact form submissions.

@10
@09
Create Date: 2026-04-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_contacts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("school_name", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_guest_contacts_email", "guest_contacts", ["email"])
    op.create_index("idx_guest_contacts_created_at", "guest_contacts", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_guest_contacts_created_at", table_name="guest_contacts")
    op.drop_index("idx_guest_contacts_email", table_name="guest_contacts")
    op.drop_table("guest_contacts")
