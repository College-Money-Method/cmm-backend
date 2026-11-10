"""Scope shown and filtered on in the admin knowledge viewer.

The list filters reuse ``scope_clause`` from ``retrieval_scope`` so the viewer
and the chat cannot disagree about who sees a chunk.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import not_, select
from sqlalchemy.orm import Session

from src.assistant.retrieval_scope import scope_clause
from src.cycles.models import Cohort
from src.schools.models import School

Visibility = Literal["public", "restricted"]


class ScopeRef(BaseModel):
    id: str
    name: str


class ChunkScopeOut(BaseModel):
    """What a chunk is limited to; ``public`` when every list is empty."""

    schools: list[ScopeRef] = []
    cohorts: list[ScopeRef] = []
    states: list[str] = []
    public: bool = True


def _names(db: Session, model, ids: set[str]) -> dict[str, str]:
    """Names for the ids that parse as uuids and still exist."""
    parsed = {}
    for raw in ids:
        try:
            parsed[uuid.UUID(raw)] = raw
        except ValueError:
            continue
    if not parsed:
        return {}
    rows = db.execute(select(model.id, model.name).where(model.id.in_(parsed))).all()
    return {parsed[row_id]: name for row_id, name in rows}


def _refs(ids: Iterable[str], names: dict[str, str], missing: str) -> list[ScopeRef]:
    return [ScopeRef(id=i, name=names.get(i, missing)) for i in ids]


def scope_labels(db: Session, rows) -> dict[uuid.UUID, ChunkScopeOut]:
    """Scope per chunk id: two batched name lookups for the whole page."""
    school_ids = {i for r in rows for i in (r.scope_school_ids or ())}
    cohort_ids = {i for r in rows for i in (r.scope_cohort_ids or ())}
    school_names = _names(db, School, school_ids)
    cohort_names = _names(db, Cohort, cohort_ids)
    # Keyed by the raw id string so a deleted or malformed id still shows up.
    school_by_raw = {str(k): v for k, v in school_names.items()}
    cohort_by_raw = {str(k): v for k, v in cohort_names.items()}
    out = {}
    for r in rows:
        schools, cohorts, states = r.scope_school_ids or [], r.scope_cohort_ids or [], r.scope_states or []
        out[r.id] = ChunkScopeOut(
            schools=_refs(schools, school_by_raw, "(removed school)"),
            cohorts=_refs(cohorts, cohort_by_raw, "(removed cohort)"),
            states=list(states),
            public=not (schools or cohorts or states),
        )
    return out


def scope_filters(db: Session, visibility: Visibility | None, school_id: uuid.UUID | None) -> list:
    """WHERE clauses for the list's visibility and school filters."""
    filters = []
    if visibility == "public":
        filters.append(scope_clause())
    elif visibility == "restricted":
        filters.append(not_(scope_clause()))
    if school_id is not None:
        school = db.get(School, school_id)
        if school is None:
            raise HTTPException(status_code=404, detail="assistant_school_not_found")
        filters.append(scope_clause(school.id, school.cohort_id, school.state))
    return filters
