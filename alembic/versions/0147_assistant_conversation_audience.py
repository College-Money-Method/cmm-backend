"""Assistant conversations: which assistant a chat belongs to

The family and counselor assistants share this table, and each endpoint must
only ever see its own rows. Existing rows are family chats, which the default
covers. The owner index leads with the audience so every list query is served
by it.

Revision ID: 0147
Revises: 0146
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("audience", sa.Text(), nullable=False, server_default="family"),
    )
    op.drop_index("idx_assistant_conversations_user", table_name="assistant_conversations")
    op.create_index(
        "idx_assistant_conversations_owner",
        "assistant_conversations",
        ["audience", "local_user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_assistant_conversations_owner", table_name="assistant_conversations")
    op.create_index(
        "idx_assistant_conversations_user",
        "assistant_conversations",
        ["local_user_id", "updated_at"],
    )
    op.drop_column("assistant_conversations", "audience")
