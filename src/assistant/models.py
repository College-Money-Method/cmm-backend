"""Tables behind the AI assistant: searchable content chunks and chat history.

``assistant_chunks`` is a derived index: every row is rebuilt from published
content by ``src.assistant.sync`` and can be dropped and regenerated at any
time. Chat history is keyed by an anonymous per-device id
(``local_user_id``) — no name, email or account is stored with it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, Computed, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base

EMBEDDING_DIMENSIONS = 1024

# Keyword side of the hybrid search: title, section heading, body and the
# family-wording questions written at sync time all feed one tsvector.
_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(heading, '') || ' ' "
    "|| text || ' ' || coalesce(enrichment, ''))"
)


class AssistantChunk(Base):
    __tablename__ = "assistant_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # "topic" | "resource" | "workshop" | "faq"
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    # Site-relative link to the canonical page; null when the source has none.
    url_path: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Questions a family would ask that this chunk answers (Haiku, at sync time).
    enrichment: Mapped[str | None] = mapped_column(Text)
    for_family: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    for_counselor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Hash of the whole source document; a change re-chunks the source.
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_SEARCH_VECTOR_SQL, persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_assistant_chunks_source", "source_type", "source_id"),
        Index("idx_assistant_chunks_search_vector", "search_vector", postgresql_using="gin"),
        # Exact scan is fine at ~1k rows; HNSW keeps it fast if the corpus grows.
        Index(
            "idx_assistant_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Random UUID the browser keeps in a cookie; the only link to a person.
    local_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="conversation",
        order_by="AssistantMessage.created_at",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("idx_assistant_conversations_user", "local_user_id", "updated_at"),)


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("assistant_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Sources shown with an assistant answer: [{n, title, url, source_type, heading}].
    sources: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    # Profile the question was asked with ({role, grade, state}); answer quality review.
    profile: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    model_id: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    conversation: Mapped[AssistantConversation] = relationship(back_populates="messages")

    __table_args__ = (Index("idx_assistant_messages_conversation", "conversation_id", "created_at"),)
