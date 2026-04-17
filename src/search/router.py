"""Global search endpoint — searches across topics, workshops, and content assets."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy import desc

from src.content.models import ContentAsset, Topic
from src.db.deps import DbDep
from src.schools.models import School
from src.workshops.models import PortalMapping, Webinar, Workshop

router = APIRouter(prefix="/api/v1/search", tags=["search"])


class SearchResult(BaseModel):
    type: Literal["topic", "workshop", "content_asset"]
    id: uuid.UUID
    title: str
    description: str | None
    slug: str | None        # topics only
    webinar_id: uuid.UUID | None  # workshops only — school-specific
    rank: float


class GlobalSearchResponse(BaseModel):
    topics: list[SearchResult]
    workshops: list[SearchResult]
    content_assets: list[SearchResult]


@router.get("", response_model=GlobalSearchResponse)
def global_search(
    q: Annotated[str, Query(min_length=1)],
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 3,
    type: Annotated[Literal["topics", "workshops", "resources"] | None, Query()] = None,
    school_slug: Annotated[str | None, Query()] = None,
) -> GlobalSearchResponse:
    """Public: full-text search across topics, workshops, and content assets."""
    tsquery = func.plainto_tsquery("english", q)

    topic_results: list[SearchResult] = []
    workshop_results: list[SearchResult] = []
    asset_results: list[SearchResult] = []

    # ── Topics ────────────────────────────────────────────────────────────────
    if type is None or type == "topics":
        rows = db.execute(
            select(
                Topic.id,
                Topic.title,
                Topic.description,
                Topic.slug,
                func.ts_rank(Topic.search_vector, tsquery).label("rank"),
            )
            .where(Topic.search_vector.op("@@")(tsquery))
            .where(Topic.status == "published")
            .order_by(desc("rank"))
            .limit(limit)
        ).all()
        topic_results = [
            SearchResult(
                type="topic", id=r.id, title=r.title, description=r.description,
                slug=r.slug, webinar_id=None, rank=r.rank,
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

        stmt = select(
            Workshop.id,
            Workshop.name,
            Workshop.description,
            func.ts_rank(Workshop.search_vector, tsquery).label("rank"),
            *(
                [webinar_subq.label("webinar_id")]
                if webinar_subq is not None
                else []
            ),
        ).where(Workshop.search_vector.op("@@")(tsquery)).order_by(desc("rank")).limit(limit)

        rows = db.execute(stmt).all()
        workshop_results = [
            SearchResult(
                type="workshop", id=r.id, title=r.name, description=r.description,
                slug=None,
                webinar_id=r.webinar_id if school_slug else None,
                rank=r.rank,
            )
            for r in rows
        ]

    # ── Content assets (resources) ────────────────────────────────────────────
    if type is None or type == "resources":
        rows = db.execute(
            select(
                ContentAsset.id,
                ContentAsset.name,
                ContentAsset.description,
                func.ts_rank(ContentAsset.search_vector, tsquery).label("rank"),
            )
            .where(ContentAsset.search_vector.op("@@")(tsquery))
            .where(ContentAsset.status == "published")
            .order_by(desc("rank"))
            .limit(limit)
        ).all()
        asset_results = [
            SearchResult(
                type="content_asset", id=r.id, title=r.name, description=r.description,
                slug=None, webinar_id=None, rank=r.rank,
            )
            for r in rows
        ]

    return GlobalSearchResponse(topics=topic_results, workshops=workshop_results, content_assets=asset_results)
