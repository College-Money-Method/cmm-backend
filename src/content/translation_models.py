"""ORM models for translation: entity cache, per-string cache, usage ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from decimal import Decimal

from sqlalchemy import Index, Integer, Numeric, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class ContentTranslation(Base):
    """Cached translation of a content entity's translatable fields.

    Keyed by (entity_type, entity_key, locale). ``source_hash`` is a SHA-256
    of the canonical JSON of the source field map; a mismatch means the source
    content has changed and the cache row must be refreshed.
    """

    __tablename__ = "content_translations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # "topic" | "page" | "asset"
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    # slug (topic/page) or UUID string (asset)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    # BCP-47 locale code e.g. "es", "zh"
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    # Translated field values, structure mirrors the source field map
    translated_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # SHA-256 hex of canonical JSON of the source fields (used for cache invalidation)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Bedrock model id used to produce this translation
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_key", "locale",
            name="uq_content_translations_entity_locale",
        ),
        Index(
            "idx_content_translations_entity_locale",
            "entity_type", "entity_key", "locale",
        ),
    )


class StringTranslation(Base):
    """Cached translation of a single visible UI/content string.

    Powers site-wide DOM string translation: every unique string is translated
    once per locale and reused across all pages (the cache key is the string,
    not the page), so shared chrome ("Start Learning", nav labels) is translated
    a single time. Keyed by (content_hash, locale).
    """

    __tablename__ = "string_translations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # SHA-256 hex of the source string (normalized: stripped of surrounding whitespace)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # BCP-47 locale code e.g. "es", "zh"
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    # Original English string (kept for debugging / cache inspection)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Bedrock model id used to produce this translation
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "content_hash", "locale",
            name="uq_string_translations_hash_locale",
        ),
        Index(
            "idx_string_translations_hash_locale",
            "content_hash", "locale",
        ),
    )


class TranslationUsage(Base):
    """One row per Bedrock translation invocation (cache MISS only) — the token
    + cost ledger. Cache hits cost nothing and write no row here, so summing this
    table gives true translation spend. `cost_usd` is computed at insert time
    from the model's per-token rates, so historical rows survive rate changes.

    Query examples:
        SELECT locale, SUM(cost_usd), SUM(output_tokens) FROM translation_usage GROUP BY locale;
        SELECT date_trunc('day', created_at) d, SUM(cost_usd) FROM translation_usage GROUP BY d;
    """

    __tablename__ = "translation_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    # What was translated: "strings" (DOM path) | "topic" | "page" | "asset"
    context: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # Number of strings / fields translated in this invocation.
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # USD cost computed from tokens × configured rates at insert time.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_translation_usage_locale", "locale"),
        Index("idx_translation_usage_created_at", "created_at"),
    )
