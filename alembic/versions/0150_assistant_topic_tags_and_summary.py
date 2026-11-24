"""Assistant review: a topic tag per question and an admin summary per conversation

``assistant_messages.topic_tag`` holds the topic a user question was classified
into (see ``src/assistant/question_topics.py``); answers keep it null.
``assistant_conversations.admin_summary`` caches the summary an admin asked for,
with when it was written and how many messages it covered (to show when stale).

Revision ID: 0150
Revises: 0149
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assistant_messages", sa.Column("topic_tag", sa.Text(), nullable=True))
    op.create_index("ix_assistant_messages_topic_tag", "assistant_messages", ["topic_tag"])
    op.add_column("assistant_conversations", sa.Column("admin_summary", sa.Text(), nullable=True))
    op.add_column(
        "assistant_conversations", sa.Column("admin_summary_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("assistant_conversations", sa.Column("admin_summary_messages", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("assistant_conversations", "admin_summary_messages")
    op.drop_column("assistant_conversations", "admin_summary_at")
    op.drop_column("assistant_conversations", "admin_summary")
    op.drop_index("ix_assistant_messages_topic_tag", table_name="assistant_messages")
    op.drop_column("assistant_messages", "topic_tag")
