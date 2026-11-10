"""The visibility rule as SQL, and how a resource's scope is read, without a database."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from src.assistant.retrieval_scope import ChunkScope, RetrievalScope, visible_chunks


def _sql(scope: RetrievalScope) -> str:
    return str(visible_chunks(scope).compile(dialect=postgresql.dialect()))


def test_scope_from_asset_sorts_and_dedupes():
    a, b = uuid.uuid4(), uuid.uuid4()
    asset = SimpleNamespace(
        schools=[SimpleNamespace(id=b), SimpleNamespace(id=a), SimpleNamespace(id=b)],
        cohorts=[],
        states=["TX", "CA", "TX"],
    )
    scope = ChunkScope.from_asset(asset)
    assert scope.school_ids == tuple(sorted([str(a), str(b)]))
    assert scope.cohort_ids == ()
    assert scope.states == ("CA", "TX")
    assert not scope.is_public


def test_equal_scopes_compare_equal_whatever_the_order():
    assert ChunkScope.of(states=["TX", "CA"]) == ChunkScope.of(states=["CA", "TX"])
    assert ChunkScope.of().is_public


def test_public_scope_has_no_context_terms():
    sql = _sql(RetrievalScope.public("family"))
    assert "cardinality" in sql
    assert "ANY" not in sql
    assert "for_family" in sql and "for_counselor" not in sql


def test_each_context_value_adds_one_or_term():
    sql = _sql(RetrievalScope("counselor", school_id=uuid.uuid4(), cohort_id=uuid.uuid4(), state="tx"))
    assert sql.count("= ANY") == 3
    assert "for_counselor" in sql

    only_school = _sql(RetrievalScope("family", school_id=uuid.uuid4()))
    assert only_school.count("= ANY") == 1
    assert "ANY (assistant_chunks.scope_school_ids)" in only_school
    assert "ANY (assistant_chunks.scope_cohort_ids)" not in only_school
