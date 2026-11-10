"""Who may see which chunk: the one visibility rule every retrieval shares.

A chunk is visible when it is written for the asker's audience and either has
no scope at all (every scope column empty) or matches the asker's school,
cohort or state. This is the chunk-side twin of ``_asset_visible_to_school`` in
``src.content.router``: additive across dimensions, and an asker with no school
sees only unscoped chunks.

Chat, the counselor chat and the admin search test all build a
``RetrievalScope`` and hand it to ``hybrid_search``; the filter pieces are
public so admin list filters reuse them instead of restating the rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import and_, func, or_

from src.assistant.models import AssistantChunk

if TYPE_CHECKING:
    from src.content.models import ContentAsset

Audience = Literal["family", "counselor"]


@dataclass(frozen=True)
class ChunkScope:
    """The schools, cohorts and states a chunk is limited to; all empty = public.

    Ids are strings (the chunk columns are ``text[]``). Tuples are sorted and
    de-duplicated so two equal scopes compare equal whatever order the
    database returned them in, which is what the sync's change check relies on.
    """

    school_ids: tuple[str, ...] = ()
    cohort_ids: tuple[str, ...] = ()
    states: tuple[str, ...] = ()

    @classmethod
    def of(cls, school_ids=(), cohort_ids=(), states=()) -> ChunkScope:
        return cls(
            tuple(sorted({str(v) for v in school_ids})),
            tuple(sorted({str(v) for v in cohort_ids})),
            tuple(sorted({str(v) for v in states})),
        )

    @classmethod
    def from_asset(cls, asset: ContentAsset) -> ChunkScope:
        return cls.of([s.id for s in asset.schools], [c.id for c in asset.cohorts], asset.states)

    @classmethod
    def from_chunk_columns(cls, school_ids, cohort_ids, states) -> ChunkScope:
        return cls.of(school_ids or (), cohort_ids or (), states or ())

    @property
    def is_public(self) -> bool:
        return not (self.school_ids or self.cohort_ids or self.states)


chunk_scope_for_asset = ChunkScope.from_asset


@dataclass(frozen=True)
class RetrievalScope:
    """Who is asking: their audience plus the school context, if any."""

    audience: Audience
    school_id: uuid.UUID | None = None
    cohort_id: uuid.UUID | None = None
    state: str | None = None

    @classmethod
    def public(cls, audience: Audience) -> RetrievalScope:
        """No school context: only chunks with no scope are visible."""
        return cls(audience)


def audience_clause(audience: Audience):
    column = AssistantChunk.for_counselor if audience == "counselor" else AssistantChunk.for_family
    return column.is_(True)


def scope_clause(school_id: uuid.UUID | None = None, cohort_id: uuid.UUID | None = None, state: str | None = None):
    """Unscoped, or matching the given school, cohort or state (each only if given)."""
    unscoped = and_(
        func.cardinality(AssistantChunk.scope_school_ids) == 0,
        func.cardinality(AssistantChunk.scope_cohort_ids) == 0,
        func.cardinality(AssistantChunk.scope_states) == 0,
    )
    terms = [unscoped]
    if school_id is not None:
        terms.append(AssistantChunk.scope_school_ids.any(str(school_id)))
    if cohort_id is not None:
        terms.append(AssistantChunk.scope_cohort_ids.any(str(cohort_id)))
    if state:
        terms.append(AssistantChunk.scope_states.any(state.upper()))
    return or_(*terms)


def visible_chunks(scope: RetrievalScope):
    """WHERE clause for the chunks this asker may be shown."""
    return and_(
        audience_clause(scope.audience),
        scope_clause(scope.school_id, scope.cohort_id, scope.state),
    )
