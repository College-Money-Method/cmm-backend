"""School SRC preview flags and fit-check prospect intake

Schools gain the preview flag, the source that created them and a preview
expiry. guest_contacts gain the school a submission was matched to, where it
came from, and the quiz answers, so fit-check leads land in the existing admin
inbox without a new table.

Revision ID: 0135
Revises: 0134
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("is_src_preview", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    op.add_column("schools", sa.Column("prospect_source", sa.Text(), nullable=True))
    op.add_column("schools", sa.Column("src_preview_expires_at",
                                       sa.TIMESTAMP(timezone=True), nullable=True))

    op.add_column("guest_contacts", sa.Column("school_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_guest_contacts_school_id", "guest_contacts", "schools",
                          ["school_id"], ["id"], ondelete="SET NULL")
    op.create_index("idx_guest_contacts_school_id", "guest_contacts", ["school_id"])
    op.add_column("guest_contacts", sa.Column("source", sa.Text(), nullable=True))
    op.add_column("guest_contacts", sa.Column("quiz_answers", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("guest_contacts", "quiz_answers")
    op.drop_column("guest_contacts", "source")
    op.drop_index("idx_guest_contacts_school_id", table_name="guest_contacts")
    op.drop_constraint("fk_guest_contacts_school_id", "guest_contacts", type_="foreignkey")
    op.drop_column("guest_contacts", "school_id")
    op.drop_column("schools", "src_preview_expires_at")
    op.drop_column("schools", "prospect_source")
    op.drop_column("schools", "is_src_preview")
