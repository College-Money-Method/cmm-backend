"""Hybrid retrieval: vector similarity + keyword match, fused by rank.

Each side catches what the other misses. Vectors match meaning across
different words ("divorced" ~ "separated parents"); keywords pin exact terms
the embedding blurs (FAFSA, CSS Profile, SAI). Reciprocal Rank Fusion merges
the two lists by position alone, so neither side's score scale matters.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from src.assistant.models import AssistantChunk

# Standard RRF constant: damps the gap between rank 1 and rank 2.
RRF_K = 60
CANDIDATES_PER_SIDE = 30
MAX_CHUNKS_PER_SOURCE = 2
DEFAULT_TOP_K = 6

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class RetrievedChunk:
    id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    title: str
    heading: str | None
    url_path: str | None
    text: str
    score: float


def reciprocal_rank_fusion(*rankings: list[uuid.UUID], k: int = RRF_K) -> dict[uuid.UUID, float]:
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return dict(scores)


def keyword_query(question: str) -> str:
    """OR of the question's words for ``to_tsquery``.

    ``websearch_to_tsquery`` ANDs every word, so a natural question ("how do
    we fill out the FAFSA if we're divorced") matches nothing. OR-ing lets
    ``ts_rank`` reward chunks that share the most words; stop words drop out
    in ``to_tsquery`` itself.
    """
    words = {w.lower() for w in _WORD_RE.findall(question) if len(w) > 1}
    return " | ".join(sorted(words))


def retrieval_query(history: list[dict], message: str, prior_questions: int = 2) -> str:
    """Text to search with: the new message plus the last few user turns.

    A follow-up or an answer to the assistant's own follow-up question ("yes,
    they're divorced") retrieves poorly alone. Two prior questions keep the
    original subject even after one round of clarification.
    """
    asked = [m["content"] for m in history if m["role"] == "user"][-prior_questions:]
    return "\n".join([*asked, message])


def _audience_filter(role: str):
    return AssistantChunk.for_counselor.is_(True) if role == "counselor" else AssistantChunk.for_family.is_(True)


def hybrid_search(
    db: Session, question: str, query_vector: list[float], role: str = "family", top_k: int = DEFAULT_TOP_K
) -> list[RetrievedChunk]:
    audience = _audience_filter(role)

    vector_ids = list(
        db.scalars(
            select(AssistantChunk.id)
            .where(audience)
            .order_by(AssistantChunk.embedding.cosine_distance(query_vector))
            .limit(CANDIDATES_PER_SIDE)
        )
    )

    keyword_ids: list[uuid.UUID] = []
    terms = keyword_query(question)
    if terms:
        tsquery = func.to_tsquery(literal_column("'english'::regconfig"), terms)
        keyword_ids = list(
            db.scalars(
                select(AssistantChunk.id)
                .where(audience, AssistantChunk.search_vector.op("@@")(tsquery))
                .order_by(func.ts_rank_cd(AssistantChunk.search_vector, tsquery).desc())
                .limit(CANDIDATES_PER_SIDE)
            )
        )

    scores = reciprocal_rank_fusion(vector_ids, keyword_ids)
    if not scores:
        return []
    rows = {c.id: c for c in db.scalars(select(AssistantChunk).where(AssistantChunk.id.in_(scores)))}

    picked: list[RetrievedChunk] = []
    per_source: dict[uuid.UUID, int] = defaultdict(int)
    for chunk_id in sorted(scores, key=scores.get, reverse=True):
        row = rows.get(chunk_id)
        if row is None or per_source[row.source_id] >= MAX_CHUNKS_PER_SOURCE:
            continue
        per_source[row.source_id] += 1
        picked.append(
            RetrievedChunk(
                row.id, row.source_type, row.source_id, row.title, row.heading, row.url_path, row.text,
                scores[chunk_id],
            )
        )
        if len(picked) == top_k:
            break
    return picked
