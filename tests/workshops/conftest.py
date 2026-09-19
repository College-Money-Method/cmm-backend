"""SQLite test schema for the webinar admin endpoints.

Same shape as ``tests/emails/conftest.py``: workshop/webinar/school tables use
Postgres-only column types (JSONB, TSVECTOR) and one Postgres-only GENERATED
column (``Webinar.duration_minutes``, built on ``EXTRACT(EPOCH FROM ...)``)
that SQLite cannot compile. Register dialect shims for the types, then clone
the ORM metadata into an isolated copy — never mutating the shared
``Base.metadata`` the real Postgres migrations run against — with that one
GENERATED clause stripped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401 - imports every model module, registering them with Base.metadata
from src.db.base import Base


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_as_text_on_sqlite(type_, compiler, **kw):
    return "TEXT"


# Only what the webinar admin endpoints touch, plus everything a hard delete
# cascades into — the tables the delete guard counts.
WEBINAR_TEST_TABLES = (
    "schools",
    "cohorts",
    "grade_sets",
    "workshops",
    "webinars",
    "cycles",
    "portal_mapping",
    "workshop_registrations",
    "email_send_log",
    # Rescheduling a webinar clears this webinar's automation claims.
    "automation_send_ledger",
    # email_send_log carries FKs to these; SQLite resolves them at create time.
    "broadcast",
    "email_automation",
    "email_template",
)


@pytest.fixture
def webinar_sessionmaker():
    """Fresh in-memory SQLite engine + sessionmaker, scoped to one test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    # SQLite ignores foreign keys unless asked, and the delete path relies on
    # ON DELETE CASCADE — without this the tests would pass on a schema that
    # leaves orphaned registrations behind in Postgres.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk_enforcement(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    test_metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name in WEBINAR_TEST_TABLES:
            table.to_metadata(test_metadata)
    test_metadata.tables["webinars"].c.duration_minutes.computed = None

    test_metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── webinar Q&A ──────────────────────────────────────────────────────────────

# Seeds for the Q&A fixtures. Everything these reach by foreign key is pulled in
# by `_qa_table_closure` — `webinar_qa_questions` alone reaches webinars,
# workshops, registrations and the video jobs an extraction points at, and hand
# listing that graph goes stale the moment a column is added upstream.
QA_SEED_TABLES = (
    "webinar_qa_syncs",
    "webinar_qa_questions",
    "webinar_qa_answer_extractions",
    "webinar_video_jobs",
)


def _qa_table_closure(seed: tuple[str, ...]) -> set[str]:
    """Seed tables plus every table reachable from them through foreign keys.

    SQLite resolves each REFERENCES clause at CREATE TABLE time, so a partial
    subset fails with NoReferencedTableError rather than quietly dropping the
    constraint.
    """
    needed: set[str] = set()
    queue = list(seed)
    while queue:
        name = queue.pop()
        if name in needed:
            continue
        table = Base.metadata.tables.get(name)
        if table is None:
            continue
        needed.add(name)
        queue.extend(fk.column.table.name for fk in table.foreign_keys)
    return needed


@pytest.fixture
def qa_sessionmaker():
    """Fresh in-memory SQLite engine holding the Q&A tables and what they reference."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    test_metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name in _qa_table_closure(QA_SEED_TABLES):
            table.to_metadata(test_metadata)
    test_metadata.tables["webinars"].c.duration_minutes.computed = None

    test_metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def qa_db(qa_sessionmaker):
    session = qa_sessionmaker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def qa_webinar(qa_db):
    """One persisted webinar with the Zoom id the sync resolves against."""
    import uuid

    from src.workshops.models import Webinar, Workshop

    workshop = Workshop(id=uuid.uuid4(), name="Paying for College")
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        webinar_name="Paying for College — Sept",
        zoom_webinar_id="83822890565",
    )
    qa_db.add_all([workshop, webinar])
    qa_db.commit()
    return webinar
