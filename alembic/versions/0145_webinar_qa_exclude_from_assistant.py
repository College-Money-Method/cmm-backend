"""Webinar Q&A: let an admin keep a question out of the assistant

Answered webinar questions feed the assistant's knowledge base automatically;
``exclude_from_assistant`` is the admin's veto for one that should not.

Revision ID: 0145
Revises: 0144
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webinar_qa_questions",
        sa.Column("exclude_from_assistant", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("webinar_qa_questions", "exclude_from_assistant")
