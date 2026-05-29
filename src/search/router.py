"""Global search endpoint — searches across topics, workshops, and content assets."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from spellchecker import SpellChecker
from sqlalchemy import func, select, or_

from src.content.models import ContentAsset, Topic
from src.db.deps import DbDep
from src.schools.models import School
from src.search.models import SearchLog
from src.workshops.models import PortalMapping, Webinar, Workshop

router = APIRouter(prefix="/api/v1/search", tags=["search"])

# distance=1: only correct single-char edits (safer — avoids mangling short domain words)
_spell = SpellChecker(distance=1)
# Teach the checker about education/financial-aid domain terms so they're never "corrected"
_spell.word_frequency.load_words([
    "fafsa", "css", "sai", "efc", "ferpa", "pell", "isir", "fseog",
    "idoc", "imrf", "ipeds", "sibling", "tuition", "stipend",
])


def _correct_query(q: str) -> str:
    """Return a spell-corrected version of q for fuzzy matching.

    Only corrects edit-distance-1 typos. Skips ALL_CAPS acronyms, short
    words (<4 chars), and non-alpha tokens to avoid mangling domain terms.
    """
    corrected = []
    for w in q.strip().split():
        if w.isupper() or len(w) < 4 or not w.isalpha():
            corrected.append(w)
            continue
        lower = w.lower()
        if lower in _spell.unknown([lower]):
            suggestion = _spell.correction(lower)
            corrected.append(suggestion if suggestion else w)
        else:
            corrected.append(w)
    return " ".join(corrected)


class SearchResult(BaseModel):
    type: Literal["topic", "workshop", "content_asset"]
    id: uuid.UUID
    title: str
    description: str | None
    headline: str | None     # ts_headline snippet with <b> highlights
    slug: str | None        # topics only
    webinar_id: uuid.UUID | None  # workshops only — school-specific
    rank: float


class GlobalSearchResponse(BaseModel):
    topics: list[SearchResult]
    workshops: list[SearchResult]
    content_assets: list[SearchResult]


# Stored vectors use setweight: title/name=A, description=B, body=D.
# ts_rank default weights: {D=0.1, C=0.2, B=0.4, A=1.0}.
# Title matches score ~0.9, description ~0.1, single body mention ~1e-20.
# Threshold separates description-level relevance from incidental body mentions.
_MIN_RANK = 0.005


@router.get("", response_model=GlobalSearchResponse)
def global_search(
    q: Annotated[str, Query(min_length=1)],
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 3,
    type: Annotated[Literal["topics", "workshops", "resources"] | None, Query()] = None,
    school_slug: Annotated[str | None, Query()] = None,
) -> GlobalSearchResponse:
    """Public: full-text search across topics, workshops, and content assets."""
    # Two complementary queries are OR-ed on every table:
    #
    # 1. simple prefix   — to_tsquery('simple', 'prod:*')
    #    Lowercases only, no stemming.  ':*' does a raw character-prefix match
    #    against stored lexemes.  The English tsvector stores 'product' for
    #    'products', and 'product' starts with 'prod' → matches partial input.
    #
    # 2. English stemmed — plainto_tsquery('english', 'products')
    #    Normalises 'products' → 'product' and matches the stored lexeme
    #    exactly.  Needed so a fully-typed inflected word still finds results
    #    even though 'products:*' (simple) wouldn't match the stored 'product'.
    words = [w for w in q.strip().split() if w]
    prefix_expr = " & ".join(w + ":*" for w in words) if words else q
    simple_tsq = func.to_tsquery("simple", prefix_expr)
    english_tsq = func.plainto_tsquery("english", q)

    corrected_q = _correct_query(q)
    corrected_tsq = func.plainto_tsquery("english", corrected_q) if corrected_q != q else english_tsq

    topic_results: list[SearchResult] = []
    workshop_results: list[SearchResult] = []
    asset_results: list[SearchResult] = []

    # ── Topics ────────────────────────────────────────────────────────────────
    if type is None or type == "topics":
        rank_expr = func.greatest(
            func.ts_rank(Topic.search_vector, english_tsq),
            func.ts_rank(Topic.search_vector, corrected_tsq),
        )
        headline_text = (
            func.coalesce(Topic.title, "") + " " +
            func.coalesce(Topic.description, "") + " " +
            func.coalesce(Topic.search_text, "")
        )
        headline_expr = func.ts_headline(
            "english", headline_text, corrected_tsq,
            "MaxFragments=1, MaxWords=18, MinWords=6, StartSel=<b>, StopSel=</b>",
        )
        inner = (
            select(
                Topic.id,
                Topic.title,
                Topic.description,
                Topic.slug,
                rank_expr.label("rank"),
                headline_expr.label("headline"),
            )
            .where(or_(
                Topic.search_vector.op("@@")(simple_tsq),
                Topic.search_vector.op("@@")(english_tsq),
                Topic.search_vector.op("@@")(corrected_tsq),
            ))
            .where(Topic.status == "published")
            .subquery()
        )
        rows = db.execute(
            select(inner)
            .where(inner.c.rank > _MIN_RANK)
            .order_by(inner.c.rank.desc())
            .limit(limit)
        ).all()
        topic_results = [
            SearchResult(
                type="topic", id=r.id, title=r.title, description=r.description,
                headline=r.headline, slug=r.slug, webinar_id=None, rank=r.rank,
            )
            for r in rows
        ]

    # ── Workshops ─────────────────────────────────────────────────────────────
    if type is None or type == "workshops":
        # Correlated scalar subquery: get the most upcoming webinar for this school
        webinar_subq = (
            select(Webinar.id)
            .join(PortalMapping, PortalMapping.webinar_id == Webinar.id)
            .join(School, School.id == PortalMapping.school_id)
            .where(School.slug == school_slug)
            .where(Webinar.workshop_id == Workshop.id)
            .order_by(Webinar.start_datetime.desc())
            .limit(1)
            .correlate(Workshop)
            .scalar_subquery()
        ) if school_slug else None

        rank_expr = func.greatest(
            func.ts_rank(Workshop.search_vector, english_tsq),
            func.ts_rank(Workshop.search_vector, corrected_tsq),
        )
        ws_headline_text = (
            func.coalesce(Workshop.name, "") + " " +
            func.coalesce(Workshop.description, "") + " " +
            func.coalesce(Workshop.search_text, "")
        )
        ws_headline_expr = func.ts_headline(
            "english", ws_headline_text, corrected_tsq,
            "MaxFragments=1, MaxWords=18, MinWords=6, StartSel=<b>, StopSel=</b>",
        )
        stmt = (
            select(
                Workshop.id,
                Workshop.name,
                Workshop.description,
                rank_expr.label("rank"),
                ws_headline_expr.label("headline"),
                *(
                    [webinar_subq.label("webinar_id")]
                    if webinar_subq is not None
                    else []
                ),
            )
            .where(or_(
                Workshop.search_vector.op("@@")(simple_tsq),
                Workshop.search_vector.op("@@")(english_tsq),
                Workshop.search_vector.op("@@")(corrected_tsq),
            ))
            .where(rank_expr > _MIN_RANK)
            .order_by(rank_expr.desc())
            .limit(limit)
        )
        rows = db.execute(stmt).all()
        workshop_results = [
            SearchResult(
                type="workshop", id=r.id, title=r.name, description=r.description,
                headline=r.headline,
                slug=None,
                webinar_id=r.webinar_id if school_slug else None,
                rank=r.rank,
            )
            for r in rows
        ]

    # ── Content assets (resources) ────────────────────────────────────────────
    if type is None or type == "resources":
        rank_expr = func.greatest(
            func.ts_rank(ContentAsset.search_vector, english_tsq),
            func.ts_rank(ContentAsset.search_vector, corrected_tsq),
        )
        ca_headline_text = (
            func.coalesce(ContentAsset.name, "") + " " +
            func.coalesce(ContentAsset.description, "") + " " +
            func.coalesce(ContentAsset.search_text, "")
        )
        ca_headline_expr = func.ts_headline(
            "english", ca_headline_text, corrected_tsq,
            "MaxFragments=1, MaxWords=18, MinWords=6, StartSel=<b>, StopSel=</b>",
        )
        inner = (
            select(
                ContentAsset.id,
                ContentAsset.name,
                ContentAsset.description,
                rank_expr.label("rank"),
                ca_headline_expr.label("headline"),
            )
            .where(or_(
                ContentAsset.search_vector.op("@@")(simple_tsq),
                ContentAsset.search_vector.op("@@")(english_tsq),
                ContentAsset.search_vector.op("@@")(corrected_tsq),
            ))
            .where(ContentAsset.status == "published")
            .subquery()
        )
        rows = db.execute(
            select(inner)
            .where(inner.c.rank > _MIN_RANK)
            .order_by(inner.c.rank.desc())
            .limit(limit)
        ).all()
        asset_results = [
            SearchResult(
                type="content_asset", id=r.id, title=r.name, description=r.description,
                headline=r.headline, slug=None, webinar_id=None, rank=r.rank,
            )
            for r in rows
        ]

    results_count = len(topic_results) + len(workshop_results) + len(asset_results)

    try:
        db.add(SearchLog(
            school_slug=school_slug,
            search_type="global_search",
            query=q,
            results_count=results_count,
        ))
        db.flush()
    except Exception:
        pass

    return GlobalSearchResponse(topics=topic_results, workshops=workshop_results, content_assets=asset_results)
