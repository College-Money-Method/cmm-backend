"""Admin view of the assistant's knowledge base: browse chunks, test retrieval.

Read-only. ``assistant_chunks`` is derived from published content by
``src.assistant.sync``, so the way to change what it says is to change the
content (or exclude a webinar question), not to edit a chunk here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from src.assistant.admin_chunk_scope import (
    ChunkScopeOut,
    ScopeOptionsOut,
    Visibility,
    scope_filters,
    scope_labels,
    scope_options,
)
from src.assistant.embeddings import EmbeddingError, embed_query
from src.assistant.family_school_scope import scope_for_school
from src.assistant.models import AssistantChunk
from src.assistant.retrieval_scope import RetrievalScope, audience_clause
from src.assistant.search import hybrid_search
from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.schools.models import School
from src.schools.src_session import school_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/assistant", tags=["assistant-admin"])


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    chunk_index: int
    title: str
    heading: str | None = None
    url_path: str | None = None
    text: str
    # Questions a family would ask that this chunk answers, written at sync time.
    enrichment: str | None = None
    for_family: bool
    for_counselor: bool
    created_at: datetime
    # Filled by the list endpoint; search-test hits leave it unset.
    scope: ChunkScopeOut | None = None


class ChunkListOut(BaseModel):
    items: list[ChunkOut]
    total: int
    limit: int
    offset: int
    # Chunks per source type for the requested audience (the whole index when none),
    # whatever the other filters.
    counts: dict[str, int]
    last_indexed_at: datetime | None = None


class SearchTestIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    audience: Literal["family", "counselor"] = "family"
    # Run as this school's chat would (a preview school sees public chunks only).
    school_id: uuid.UUID | None = None


class SearchTestHit(ChunkOut):
    rank: int
    # Reciprocal-rank-fusion score of the vector and keyword rankings.
    score: float
    # Cosine similarity of the chunk to the question, 0–1.
    similarity: float | None = None


class SearchTestOut(BaseModel):
    items: list[SearchTestHit]


@router.get("/scope-options", response_model=ScopeOptionsOut)
def list_scope_options(
    _admin: AdminDep, db: DbDep, audience: Literal["family", "counselor"] | None = Query(None)
) -> ScopeOptionsOut:
    """The cohorts and states the restricted filters can pick from."""
    return scope_options(db, audience)


@router.get("/chunks", response_model=ChunkListOut)
def list_chunks(
    _admin: AdminDep,
    db: DbDep,
    source_type: str | None = Query(None, description="topic, resource, workshop, faq, webinar_qa"),
    audience: Literal["family", "counselor"] | None = Query(None),
    visibility: Annotated[Visibility | None, Query(description="public = no scope, restricted = scoped")] = None,
    school_id: Annotated[uuid.UUID | None, Query(description="Chunks this school's members would retrieve")] = None,
    cohort_id: Annotated[uuid.UUID | None, Query(description="Chunks limited to this cohort")] = None,
    state: Annotated[str | None, Query(pattern="^[A-Za-z]{2}$", description="Chunks limited to this state")] = None,
    search: str | None = Query(None, description="Substring of title, heading, text or generated questions"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ChunkListOut:
    """Chunks grouped by source, in the order they appear on their page."""
    filters = []
    if source_type:
        filters.append(AssistantChunk.source_type == source_type)
    if audience:
        filters.append(audience_clause(audience))
    filters.extend(scope_filters(db, visibility, school_id, cohort_id, state))
    if search and search.strip():
        term = search.strip()
        filters.append(
            or_(
                *(
                    column.icontains(term, autoescape=True)
                    for column in (
                        AssistantChunk.title,
                        AssistantChunk.heading,
                        AssistantChunk.text,
                        AssistantChunk.enrichment,
                    )
                )
            )
        )

    total = db.scalar(select(func.count()).select_from(AssistantChunk).where(*filters)) or 0
    rows = db.scalars(
        select(AssistantChunk)
        .where(*filters)
        .order_by(AssistantChunk.source_type, AssistantChunk.title, AssistantChunk.source_id, AssistantChunk.chunk_index)
        .limit(limit)
        .offset(offset)
    ).all()
    audience_only = [audience_clause(audience)] if audience else []
    counts = dict(
        db.execute(
            select(AssistantChunk.source_type, func.count())
            .where(*audience_only)
            .group_by(AssistantChunk.source_type)
        ).all()
    )
    scopes = scope_labels(db, rows)
    return ChunkListOut(
        items=[ChunkOut.model_validate(r).model_copy(update={"scope": scopes[r.id]}) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
        last_indexed_at=db.scalar(select(func.max(AssistantChunk.created_at))),
    )


@router.post("/search-test", response_model=SearchTestOut)
def search_test(body: SearchTestIn, _admin: AdminDep, db: DbDep) -> SearchTestOut:
    """What the chat would hand the model for this question, best first.

    Runs the same embedding and hybrid search as the first question in a new
    chat for that audience and school (no earlier turns), so it costs one query
    embedding. A preview school gets the public scope, as it would in chat.
    """
    if body.school_id is None:
        scope = RetrievalScope.public(body.audience)
    else:
        school = db.get(School, body.school_id)
        if school is None:
            raise HTTPException(status_code=404, detail="assistant_school_not_found")
        scope = scope_for_school(school, body.audience, school_mode(school))
    try:
        vector = embed_query(body.question)
    except EmbeddingError:
        logger.exception("assistant admin: query embedding failed")
        raise HTTPException(status_code=503, detail="assistant_unavailable") from None
    hits = hybrid_search(db, body.question, vector, scope)
    if not hits:
        return SearchTestOut(items=[])

    ids = [h.id for h in hits]
    rows = {c.id: c for c in db.scalars(select(AssistantChunk).where(AssistantChunk.id.in_(ids)))}
    similarity = dict(
        db.execute(
            select(AssistantChunk.id, 1 - AssistantChunk.embedding.cosine_distance(vector)).where(
                AssistantChunk.id.in_(ids)
            )
        ).all()
    )
    return SearchTestOut(
        items=[
            SearchTestHit(
                **ChunkOut.model_validate(rows[h.id]).model_dump(),
                rank=rank,
                score=h.score,
                similarity=float(similarity[h.id]) if similarity.get(h.id) is not None else None,
            )
            for rank, h in enumerate(hits, start=1)
            if h.id in rows
        ]
    )
