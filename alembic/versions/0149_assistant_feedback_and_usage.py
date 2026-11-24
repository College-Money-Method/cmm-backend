"""Assistant feedback and usage: answer ratings, chat school/card, soft delete

``assistant_message_feedback`` holds a counselor's rating, "wrong or out of
date" flag and comment on an answer. ``assistant_conversations`` gains the
school a counselor chat ran for, the Hub job card it started from, and
``deleted_at`` so a counselor's delete hides a chat without losing it. Existing
counselor chats get their school from the owner's current role (best effort).

Revision ID: 0149
Revises: 0148
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("school_id", sa.Uuid(), sa.ForeignKey("schools.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("assistant_conversations", sa.Column("card_type", sa.Text(), nullable=True))
    op.add_column("assistant_conversations", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_index("ix_assistant_conversations_school_id", "assistant_conversations", ["school_id"])
    op.execute(
        """
        UPDATE assistant_conversations AS c
        SET school_id = r.school_id
        FROM user_roles AS r
        WHERE c.audience = 'counselor' AND c.school_id IS NULL
          AND r.user_id = c.local_user_id AND r.school_id IS NOT NULL
        """
    )
    op.create_table(
        "assistant_message_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "message_id", sa.Uuid(), sa.ForeignKey("assistant_messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("local_user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("flag", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "local_user_id", name="uq_assistant_feedback_message_user"),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_assistant_feedback_rating"),
        sa.CheckConstraint("flag IN ('wrong_or_outdated')", name="ck_assistant_feedback_flag"),
    )


def downgrade() -> None:
    op.drop_table("assistant_message_feedback")
    op.drop_index("ix_assistant_conversations_school_id", table_name="assistant_conversations")
    op.drop_column("assistant_conversations", "deleted_at")
    op.drop_column("assistant_conversations", "card_type")
    op.drop_column("assistant_conversations", "school_id")
