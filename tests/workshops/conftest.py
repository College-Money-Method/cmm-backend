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
