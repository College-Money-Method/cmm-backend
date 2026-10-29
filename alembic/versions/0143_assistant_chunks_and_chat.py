"""AI assistant: pgvector chunk index, anonymous chat history, shared password

* ``vector`` extension (Supabase ships it; not installed until now).
* ``assistant_chunks``: derived search index over published content
  (embedding + generated tsvector for hybrid search). Safe to truncate; the
  sync job rebuilds it.
* ``assistant_conversations`` / ``assistant_messages``: chat history keyed by
  an anonymous per-device id.
* ``app_config.assistant_password_hash`` / ``_updated_at``: the shared password.

Revision ID: 0143
Revises: 0142
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None

_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(heading, '') || ' ' "
    "|| text || ' ' || coalesce(enrichment, ''))"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("app_config", sa.Column("assistant_password_hash", sa.Text(), nullable=True))
    op.add_column(
        "app_config",
        sa.Column("assistant_password_updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "assistant_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("url_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("enrichment", sa.Text(), nullable=True),
        sa.Column("for_family", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("for_counselor", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), sa.Computed(_SEARCH_VECTOR_SQL, persisted=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_assistant_chunks_source", "assistant_chunks", ["source_type", "source_id"])
    op.create_index(
        "idx_assistant_chunks_search_vector", "assistant_chunks", ["search_vector"], postgresql_using="gin"
    )
    op.create_index(
        "idx_assistant_chunks_embedding",
        "assistant_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("local_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_assistant_conversations_user", "assistant_conversations", ["local_user_id", "updated_at"]
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("profile", postgresql.JSONB(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_assistant_messages_conversation", "assistant_messages", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_conversations")
    op.drop_table("assistant_chunks")
    op.drop_column("app_config", "assistant_password_updated_at")
    op.drop_column("app_config", "assistant_password_hash")
    # The extension is left installed: other objects may come to depend on it.
