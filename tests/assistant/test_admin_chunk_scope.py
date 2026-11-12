"""Admin knowledge viewer: scope labels, list filters and the scoped search test."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.assistant import admin_router
from src.assistant.admin_chunk_scope import scope_filters, scope_labels, scope_options
from src.assistant.retrieval_scope import RetrievalScope
from src.auth.deps import require_admin
from src.auth.schemas import CurrentUser
from src.cycles.models import Cohort
from src.main import app
from src.schools.models import School
from tests.schools.src_test_support import install, make_session_factory, uid

SCHOOL, PREVIEW, COHORT = uid(1), uid(2), uid(50)


@pytest.fixture
def factory():
    factory = make_session_factory()
    with factory() as db:
        db.add(Cohort(id=COHORT, name="2026 West"))
        db.add_all([
            School(id=SCHOOL, name="Branson", slug="branson", state="TX", cohort_id=COHORT, is_current_customer=True),
            School(id=PREVIEW, name="Preview High", slug="preview-high", is_src_preview=True),
        ])
        db.commit()
    return factory


def _row(schools=(), cohorts=(), states=()):
    return SimpleNamespace(
        id=uuid.uuid4(), scope_school_ids=[str(s) for s in schools],
        scope_cohort_ids=[str(c) for c in cohorts], scope_states=list(states),
    )


def test_scope_labels_resolve_names_and_mark_missing_ids(factory):
    gone = uuid.uuid4()
    public, scoped, stale = _row(), _row([SCHOOL], [COHORT], ["TX"]), _row([gone])
    with factory() as db:
        labels = scope_labels(db, [public, scoped, stale])

    assert labels[public.id].public is True
    got = labels[scoped.id]
    assert not got.public
    assert [(s.id, s.name) for s in got.schools] == [(str(SCHOOL), "Branson")]
    assert [c.name for c in got.cohorts] == ["2026 West"]
    assert got.states == ["TX"]
    assert [(s.id, s.name) for s in labels[stale.id].schools] == [(str(gone), "(removed school)")]


def test_scope_labels_tolerate_malformed_ids(factory):
    row = _row()
    row.scope_school_ids = ["not-a-uuid"]
    with factory() as db:
        assert scope_labels(db, [row])[row.id].schools[0].name == "(removed school)"


def _sql(clauses) -> str:
    return " AND ".join(str(c.compile(dialect=postgresql.dialect())) for c in clauses)


def test_visibility_filters(factory):
    with factory() as db:
        assert scope_filters(db, None, None) == []
        assert _sql(scope_filters(db, "public", None)).startswith("cardinality")
        assert _sql(scope_filters(db, "restricted", None)).startswith("NOT")


def test_school_filter_uses_the_schools_cohort_and_state(factory):
    with factory() as db:
        sql = _sql(scope_filters(db, None, SCHOOL))
        assert sql.count("= ANY") == 3
        with pytest.raises(HTTPException) as exc:
            scope_filters(db, None, uuid.uuid4())
        assert exc.value.status_code == 404


def test_cohort_and_state_filters_match_chunks_limited_to_them(factory):
    with factory() as db:
        sql = _sql(scope_filters(db, "restricted", None, COHORT, "tx"))
    assert sql.startswith("NOT")
    assert "scope_cohort_ids" in sql.split("AND")[-2]
    assert "scope_states" in sql.split("AND")[-1]
    assert sql.count("= ANY") == 2


def test_scope_options_list_distinct_cohorts_and_states(factory, monkeypatch):
    values = {"scope_cohort_ids": [str(COHORT), "gone"], "scope_states": ["CA", "TX"]}
    seen = []

    def fake_distinct(db, column, audience):
        seen.append(audience)
        return values[column.key]

    monkeypatch.setattr("src.assistant.admin_chunk_scope._distinct_values", fake_distinct)
    with factory() as db:
        got = scope_options(db, "counselor")
    assert [(c.id, c.name) for c in got.cohorts] == [("gone", "(removed cohort)"), (str(COHORT), "2026 West")]
    assert got.states == ["CA", "TX"]
    assert seen == ["counselor", "counselor"]


@pytest.fixture
def client(factory, monkeypatch):
    client = install(factory)
    app.dependency_overrides[require_admin] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", school_id=None, school_role=None, email="a@example.com"
    )
    seen = []
    monkeypatch.setattr(admin_router, "embed_query", lambda q: [0.0])
    monkeypatch.setattr(admin_router, "hybrid_search", lambda db, q, v, scope: seen.append(scope) or [])
    yield client, seen
    app.dependency_overrides.clear()


URL = "/api/v1/admin/assistant/search-test"


def test_search_test_without_school_is_public_for_the_audience(client):
    http, seen = client
    assert http.post(URL, json={"question": "q", "audience": "counselor"}).status_code == 200
    assert seen == [RetrievalScope.public("counselor")]


def test_search_test_with_school_uses_its_scope(client):
    http, seen = client
    assert http.post(URL, json={"question": "q", "audience": "counselor", "school_id": str(SCHOOL)}).status_code == 200
    assert seen == [RetrievalScope("counselor", SCHOOL, COHORT, "TX")]


def test_search_test_preview_school_gets_public_scope(client):
    http, seen = client
    http.post(URL, json={"question": "q", "audience": "family", "school_id": str(PREVIEW)})
    assert seen == [RetrievalScope.public("family")]


def test_search_test_unknown_school_is_404(client):
    http, seen = client
    r = http.post(URL, json={"question": "q", "audience": "family", "school_id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert seen == []
