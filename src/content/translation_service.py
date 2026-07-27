"""Cache-aware translation pipeline for content entities.

Resolves entity → translatable field map → SHA-256 hash → cache lookup →
Bedrock translation (on miss) → UPSERT → return.

Supports entity_type ∈ {"topic", "page", "asset"}.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from decimal import Decimal

from src.content.bedrock_translation import TranslationError, TranslationOutput, translate_fields
from src.content.models import ContentAsset, Faq, Topic
from src.content.translation_models import ContentTranslation, StringTranslation, TranslationUsage
from src.pages.models import Page
from src.config import settings

logger = logging.getLogger(__name__)

# Translatable field names per entity type (only non-null/non-empty values are sent).
_TOPIC_FIELDS = ("title", "description", "summary", "summary_items", "content", "action_items", "faqs")
_PAGE_FIELDS = ("title", "content", "meta_title", "meta_description")
_ASSET_FIELDS = (
    "name", "description", "summary", "content",
    "why_important", "how_to_use", "action_items", "faqs", "objectives",
)


# ── Entity resolution ─────────────────────────────────────────────────────────

def _resolve_topic(db: Session, key: str) -> dict[str, Any]:
    topic: Topic | None = db.scalars(
        select(Topic).where(Topic.slug == key, Topic.status == "published")
    ).first()
    if topic is None:
        raise HTTPException(status_code=404, detail=f"Topic '{key}' not found or not published")

    # Load relationship collections (lazy-loaded on access within session)
    faq_list = [{"question": f.question, "answer": f.answer} for f in topic.faqs]

    raw: dict[str, Any] = {
        "title": topic.title,
        "description": topic.description,
        "summary": topic.summary,
        # summary_items: modern JSONB array of key-takeaway strings (list[str]).
        # Only include when non-empty; the model is prompted to preserve list structure
        # and translate text values only (see _build_system_prompt rule 4).
        "summary_items": topic.summary_items,
        "content": topic.content,
        "action_items": topic.action_items,
        "faqs": faq_list,
    }
    return {k: v for k, v in raw.items() if k in _TOPIC_FIELDS and v not in (None, "", [], {})}


def _resolve_page(db: Session, key: str) -> dict[str, Any]:
    page: Page | None = db.scalars(
        select(Page).where(Page.slug == key, Page.status == "published")
    ).first()
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page '{key}' not found or not published")

    raw: dict[str, Any] = {
        "title": page.title,
        "content": page.content,
        "meta_title": page.meta_title,
        "meta_description": page.meta_description,
    }
    return {k: v for k, v in raw.items() if k in _PAGE_FIELDS and v not in (None, "", [], {})}


def _resolve_asset(db: Session, key: str) -> dict[str, Any]:
    # key is a UUID string
    try:
        asset_id = uuid.UUID(key)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Asset key must be a valid UUID, got '{key}'")

    asset: ContentAsset | None = db.scalars(
        select(ContentAsset).where(
            ContentAsset.id == asset_id,
            ContentAsset.status == "published",
        )
    ).first()
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset '{key}' not found or not published")

    faq_list = [{"question": f.question, "answer": f.answer} for f in asset.faqs]
    objective_list = [obj.name for obj in asset.objectives]

    raw: dict[str, Any] = {
        "name": asset.name,
        "description": asset.description,
        "summary": asset.summary,
        "content": asset.content,
        "why_important": asset.why_important,
        "how_to_use": asset.how_to_use,
        "action_items": asset.action_items,
        "faqs": faq_list,
        "objectives": objective_list,
    }
    return {k: v for k, v in raw.items() if k in _ASSET_FIELDS and v not in (None, "", [], {})}


_RESOLVERS = {
    "topic": _resolve_topic,
    "page": _resolve_page,
    "asset": _resolve_asset,
}


# ── Hash ──────────────────────────────────────────────────────────────────────

def _compute_source_hash(fields: dict[str, Any]) -> str:
    """SHA-256 of canonical (sorted-keys) JSON encoding of the field map."""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_string(text: str) -> str:
    """SHA-256 of a single source string (matches the frontend cache key)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_cost_usd(input_tokens: int, output_tokens: int) -> Decimal:
    """USD cost of one invocation from configured per-1M-token Haiku rates."""
    cost = (
        input_tokens * settings.bedrock_haiku_input_usd_per_mtok
        + output_tokens * settings.bedrock_haiku_output_usd_per_mtok
    ) / 1_000_000
    return Decimal(str(round(cost, 6)))


def _record_usage(
    db: Session, context: str, locale: str, out: TranslationOutput, item_count: int
) -> None:
    """Append a translation_usage ledger row for one Bedrock invocation (no commit).
    Skipped for empty/zero-token invocations."""
    if out.input_tokens == 0 and out.output_tokens == 0:
        return
    db.add(
        TranslationUsage(
            id=uuid.uuid4(),
            context=context,
            locale=locale,
            model_id=out.model_id,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            item_count=item_count,
            cost_usd=_compute_cost_usd(out.input_tokens, out.output_tokens),
        )
    )


# Batch sizing. Per-call Haiku latency grows with batch size (a 50-string batch
# is far slower than several small batches run in parallel), so we keep batches
# SMALL and fan them out across the worker pool. Benchmarks on this account:
# 120 strings as 3×50 batches = ~48s, but as 15×8 batches (8 workers) = ~8s.
# `char_budget` additionally splits long content (article paragraphs) so no
# single call blows past Haiku's output-token cap.
_STRING_BATCH_CHAR_BUDGET = 3000
_STRING_BATCH_MAX_COUNT = 8

# Concurrent Bedrock batches per request. Bedrock calls are I/O-bound; running
# many small batches in a thread pool is what delivers the speedup above.
_STRING_TRANSLATE_MAX_WORKERS = 8


def _chunk_strings(texts: list[str]) -> list[list[str]]:
    """Greedily group strings into batches bounded by char budget and count."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for text in texts:
        length = len(text)
        # A single oversized string gets its own batch.
        if current and (
            current_chars + length > _STRING_BATCH_CHAR_BUDGET
            or len(current) >= _STRING_BATCH_MAX_COUNT
        ):
            batches.append(current)
            current, current_chars = [], 0
        current.append(text)
        current_chars += length
    if current:
        batches.append(current)
    return batches


def translate_strings(
    db: Session,
    locale: str,
    texts: list[str],
) -> dict[str, str]:
    """Translate a batch of standalone strings, returning {source: translated}.

    Per-string cached in ``string_translations`` (keyed by content_hash+locale),
    so a given string is translated by Bedrock at most once per locale and reused
    across every page. Only cache-miss strings hit Bedrock, batched by size.

    Raises:
        HTTPException 502 if a Bedrock batch fails (partial cache fills persist).
    """
    # Normalize + dedupe; ignore blank/whitespace-only.
    unique: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        text = raw.strip()
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    if not unique:
        return {}

    hash_by_text = {t: _hash_string(t) for t in unique}

    # Cache lookup for this locale.
    cached_rows = db.scalars(
        select(StringTranslation).where(
            StringTranslation.locale == locale,
            StringTranslation.content_hash.in_(list(hash_by_text.values())),
        )
    ).all()
    cached = {row.content_hash: row.translated_text for row in cached_rows}

    result: dict[str, str] = {}
    misses: list[str] = []
    for text in unique:
        h = hash_by_text[text]
        if h in cached:
            result[text] = cached[h]
        else:
            misses.append(text)

    # Translate misses in size-bounded batches, running the batches CONCURRENTLY.
    # Bedrock calls are I/O-bound and the SDK's HTTP client is thread-safe, so a
    # thread pool turns page latency from sum-of-batches into ~slowest-batch.
    # Each batch maps hash → text so the model returns hash → translated
    # (avoids collisions / ordering issues). DB upserts stay on the main thread
    # (the SQLAlchemy session is not thread-safe).
    batches = _chunk_strings(misses)
    if batches:
        failure: TranslationError | None = None
        max_workers = min(len(batches), _STRING_TRANSLATE_MAX_WORKERS)

        def _run_batch(batch: list[str]) -> TranslationOutput:
            field_map = {hash_by_text[t]: t for t in batch}
            return translate_fields(field_map, locale)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_batch = {executor.submit(_run_batch, b): b for b in batches}
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    out = future.result()
                except TranslationError as exc:
                    logger.error("Bedrock string translation failed: %s", exc)
                    failure = exc
                    continue
                for text in batch:
                    h = hash_by_text[text]
                    value = out.fields.get(h)
                    # Fall back to source on a missing / non-string / blank value.
                    if not isinstance(value, str) or not value.strip():
                        value = text
                    result[text] = value
                    _upsert_string_translation(db, h, locale, text, value)
                # Ledger row per invocation (main thread — session not thread-safe).
                _record_usage(db, "strings", locale, out, len(batch))

        # Persist whatever succeeded (partial cache fill) before surfacing failure.
        db.commit()
        if failure is not None:
            raise HTTPException(
                status_code=502, detail="Translation service temporarily unavailable"
            ) from failure
    else:
        db.commit()

    return result


def _upsert_string_translation(
    db: Session, content_hash: str, locale: str, source_text: str, translated_text: str
) -> None:
    """Insert or refresh a single string-translation cache row (no commit)."""
    stmt = (
        pg_insert(StringTranslation)
        .values(
            id=uuid.uuid4(),
            content_hash=content_hash,
            locale=locale,
            source_text=source_text,
            translated_text=translated_text,
            model_id=settings.bedrock_haiku_model_id,
        )
        .on_conflict_do_update(
            constraint="uq_string_translations_hash_locale",
            set_={
                "translated_text": translated_text,
                "source_text": source_text,
                "model_id": settings.bedrock_haiku_model_id,
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)


# ── Public pipeline ───────────────────────────────────────────────────────────

def get_or_create_translation(
    db: Session,
    entity_type: str,
    key: str,
    locale: str,
) -> dict[str, Any]:
    """Return cached or freshly translated fields for the given entity + locale.

    Returns:
        dict with keys: cached (bool), fields (dict of translated values).

    Raises:
        HTTPException 404 if entity not found / not published.
        HTTPException 502 if Bedrock call fails.
    """
    resolver = _RESOLVERS[entity_type]
    field_map = resolver(db, key)

    if not field_map:
        raise HTTPException(
            status_code=422,
            detail=f"{entity_type} '{key}' has no translatable content",
        )

    source_hash = _compute_source_hash(field_map)

    # Cache lookup
    existing: ContentTranslation | None = db.scalars(
        select(ContentTranslation).where(
            ContentTranslation.entity_type == entity_type,
            ContentTranslation.entity_key == key,
            ContentTranslation.locale == locale,
        )
    ).first()

    if existing is not None and existing.source_hash == source_hash:
        logger.info("Translation cache HIT: %s/%s/%s", entity_type, key, locale)
        return {"cached": True, "fields": existing.translated_fields}

    # Cache miss (or stale) — call Bedrock
    logger.info(
        "Translation cache MISS: %s/%s/%s (stale=%s)",
        entity_type, key, locale, existing is not None,
    )
    try:
        out = translate_fields(field_map, locale)
    except TranslationError as exc:
        # Log full detail server-side (may include Bedrock status codes / partial output).
        # Return a generic message to the client to avoid leaking internal error detail.
        logger.error("Bedrock translation failed: %s", exc)
        raise HTTPException(status_code=502, detail="Translation service temporarily unavailable") from exc

    translated_fields = out.fields
    _record_usage(db, entity_type, locale, out, len(field_map))

    # UPSERT — insert or update on conflict
    stmt = (
        pg_insert(ContentTranslation)
        .values(
            id=uuid.uuid4(),
            entity_type=entity_type,
            entity_key=key,
            locale=locale,
            translated_fields=translated_fields,
            source_hash=source_hash,
            model_id=settings.bedrock_haiku_model_id,
        )
        .on_conflict_do_update(
            constraint="uq_content_translations_entity_locale",
            set_={
                "translated_fields": translated_fields,
                "source_hash": source_hash,
                "model_id": settings.bedrock_haiku_model_id,
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)
    db.commit()

    return {"cached": False, "fields": translated_fields}
