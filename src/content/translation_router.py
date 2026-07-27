"""Translation endpoint: GET /api/v1/content/translations/{entity_type}/{key}

Public endpoint — no auth required. Returns translated content fields from
Postgres cache (instant) or Bedrock (blocking on cache miss).

API contract (fixed — both phases depend on this):
  200 → { entity_type, key, locale, cached: bool, fields: { <field>: <value> } }
  400 → entity_type invalid | tl missing/unsupported/en
  404 → entity not found or not published
  502 → Bedrock translation service error
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.config import SUPPORTED_LOCALES
from src.content.translation_service import get_or_create_translation, translate_strings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/content/translations", tags=["translations"])

_VALID_ENTITY_TYPES = {"topic", "page", "asset"}

# Upper bound on strings per request — guards against abuse / runaway payloads.
_MAX_STRINGS_PER_REQUEST = 500


class TranslationResponse(BaseModel):
    entity_type: str
    key: str
    locale: str
    cached: bool
    fields: dict


class StringTranslateRequest(BaseModel):
    locale: str
    texts: list[str] = Field(default_factory=list)


class StringTranslateResponse(BaseModel):
    locale: str
    translations: dict[str, str]


def _validate_locale(tl: str) -> None:
    if not tl:
        raise HTTPException(status_code=400, detail="Locale is required")
    if tl == "en":
        raise HTTPException(status_code=400, detail="'en' is the source language")
    if tl not in SUPPORTED_LOCALES:
        raise HTTPException(
            status_code=400,
            detail=f"Locale '{tl}' is not supported. Supported: {sorted(SUPPORTED_LOCALES)}",
        )


@router.get("/{entity_type}/{key}", response_model=TranslationResponse)
def get_translation(
    entity_type: str,
    key: str,
    tl: str = Query(..., description="Target locale code e.g. 'es', 'zh'"),
    db: DbDep = None,
) -> TranslationResponse:
    """Return translated fields for the given entity.

    - **entity_type**: one of ``topic``, ``page``, ``asset``
    - **key**: slug (topic/page) or UUID string (asset)
    - **tl**: target locale; must be a supported non-English locale
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"entity_type must be one of {sorted(_VALID_ENTITY_TYPES)}, got '{entity_type}'",
        )

    if not tl:
        raise HTTPException(status_code=400, detail="Query param 'tl' is required")

    if tl == "en":
        raise HTTPException(status_code=400, detail="'tl=en' is not supported; content is already in English")

    if tl not in SUPPORTED_LOCALES:
        raise HTTPException(
            status_code=400,
            detail=f"Locale '{tl}' is not supported. Supported: {sorted(SUPPORTED_LOCALES)}",
        )

    result = get_or_create_translation(db=db, entity_type=entity_type, key=key, locale=tl)

    return TranslationResponse(
        entity_type=entity_type,
        key=key,
        locale=tl,
        cached=result["cached"],
        fields=result["fields"],
    )


@router.post("/strings", response_model=StringTranslateResponse)
def translate_strings_batch(
    payload: StringTranslateRequest,
    db: DbDep = None,
) -> StringTranslateResponse:
    """Translate a batch of standalone strings — the site-wide DOM translation path.

    Returns a ``{ source: translated }`` map. Per-string cached, so only unseen
    strings hit Bedrock. Strings beyond the per-request cap are ignored.

    - **locale**: target locale; must be a supported non-English locale
    - **texts**: source strings to translate (deduped server-side)
    """
    _validate_locale(payload.locale)

    if not payload.texts:
        return StringTranslateResponse(locale=payload.locale, translations={})

    translations = translate_strings(
        db=db,
        locale=payload.locale,
        texts=payload.texts[:_MAX_STRINGS_PER_REQUEST],
    )
    return StringTranslateResponse(locale=payload.locale, translations=translations)
