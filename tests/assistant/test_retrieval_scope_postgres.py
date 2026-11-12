"""The visibility rule against real Postgres arrays (opt-in).

SQLite stores the scope as JSON, so only Postgres proves ``= ANY(text[])`` and
``cardinality`` behave. Set ``ASSISTANT_TEST_DATABASE_URL`` to a migrated
database; every row is inserted in a transaction that is rolled back.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import src.main  # noqa: F401  (registers every model)
from src.assistant.models import AssistantChunk
from src.assistant.retrieval_scope import RetrievalScope, visible_chunks
from src.assistant.search import hybrid_search

URL = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="ASSISTANT_TEST_DATABASE_URL not set")

SCHOOL, OTHER_SCHOOL, COHORT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def session():
    engine = create_engine(URL)
    with engine.connect() as conn:
        txn = conn.begin()
        db = Session(bind=conn)
        try:
            yield db
        finally:
            db.close()
            txn.rollback()
    engine.dispose()


def _add(db: Session, label: str, *, schools=(), cohorts=(), states=(), family=True, counselor=True) -> uuid.UUID:
    source_id = uuid.uuid4()
    db.add(
        AssistantChunk(
            source_type="resource", source_id=source_id, chunk_index=0, title=label, text="zzqscope guidance",
            for_family=family, for_counselor=counselor, content_hash=label, embedding=VECTOR,
            scope_school_ids=[str(s) for s in schools], scope_cohort_ids=[str(c) for c in cohorts],
            scope_states=list(states),
        )
    )
    return source_id


@pytest.fixture
def ids(session):
    found = {
        "public": _add(session, "public"),
        "school": _add(session, "school", schools=[SCHOOL]),
        "cohort": _add(session, "cohort", cohorts=[COHORT]),
        "state": _add(session, "state", states=["CA"]),
        "counselor_only": _add(session, "counselor_only", family=False),
    }
    session.flush()
    return found


def _visible(session, ids, scope) -> set[str]:
    rows = session.scalars(
        select(AssistantChunk.source_id).where(visible_chunks(scope), AssistantChunk.source_id.in_(ids.values()))
    )
    by_id = {v: k for k, v in ids.items()}
    return {by_id[r] for r in rows}


def test_visibility_matrix(session, ids):
    family = "family"
    assert _visible(session, ids, RetrievalScope.public(family)) == {"public"}
    assert _visible(session, ids, RetrievalScope(family, school_id=SCHOOL)) == {"public", "school"}
    assert _visible(session, ids, RetrievalScope(family, school_id=OTHER_SCHOOL)) == {"public"}
    assert _visible(session, ids, RetrievalScope(family, school_id=OTHER_SCHOOL, cohort_id=COHORT)) == {"public", "cohort"}
    assert _visible(session, ids, RetrievalScope(family, school_id=OTHER_SCHOOL, state="ca")) == {"public", "state"}
    full = RetrievalScope(family, school_id=SCHOOL, cohort_id=COHORT, state="CA")
    assert _visible(session, ids, full) == {"public", "school", "cohort", "state"}


def test_counselor_audience_sees_counselor_only_chunk(session, ids):
    assert _visible(session, ids, RetrievalScope.public("counselor")) == {"public", "counselor_only"}


def test_hybrid_search_applies_the_same_rule(session, ids):
    hits = hybrid_search(session, "zzqscope guidance", VECTOR, RetrievalScope("family", school_id=OTHER_SCHOOL), top_k=50)
    mine = {h.source_id for h in hits} & set(ids.values())
    assert mine == {ids["public"]}


def test_admin_list_filters_match_what_chat_retrieves(session, ids):
    from src.assistant.admin_chunk_scope import scope_filters
    from src.cycles.models import Cohort
    from src.schools.models import School

    session.add(Cohort(id=COHORT, name=f"cohort-{COHORT}"))
    session.add(School(id=SCHOOL, name=f"school-{SCHOOL}", slug=f"s-{SCHOOL}", cohort_id=COHORT, state="CA"))
    session.flush()

    def listed(visibility, school_id, cohort_id=None, state=None) -> set[str]:
        rows = session.scalars(
            select(AssistantChunk.source_id).where(
                AssistantChunk.source_id.in_(ids.values()),
                *scope_filters(session, visibility, school_id, cohort_id, state),
            )
        )
        by_id = {v: k for k, v in ids.items()}
        return {by_id[r] for r in rows}

    assert listed("public", None) == {"public", "counselor_only"}
    assert listed("restricted", None) == {"school", "cohort", "state"}
    assert listed(None, SCHOOL) == {"public", "school", "cohort", "state", "counselor_only"}
    assert listed("restricted", SCHOOL) == {"school", "cohort", "state"}
    assert listed("restricted", None, cohort_id=COHORT) == {"cohort"}
    assert listed("restricted", None, state="ca") == {"state"}
    assert listed(None, None, cohort_id=uuid.uuid4()) == set()


def test_scope_options_list_values_chunks_are_limited_to(session, ids):
    from src.assistant.admin_chunk_scope import scope_options

    got = scope_options(session, "family")
    assert str(COHORT) in {c.id for c in got.cohorts}
    assert "CA" in got.states
    assert got.states == sorted(got.states)
