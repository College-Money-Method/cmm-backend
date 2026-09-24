"""In-memory SQLite fixtures for the job-state and webhook-trigger tests.

Same shape as ``tests/emails/conftest.py``: Workshop/Webinar use Postgres-only
column types (JSONB, TSVECTOR) plus one GENERATED column
(``Webinar.duration_minutes``) that SQLite cannot compile, so the types get
dialect shims and the metadata is cloned into an isolated copy with that
computed clause stripped. ``Base.metadata`` itself is never mutated — the real
Postgres migrations read it.

What SQLite does reproduce faithfully is the UNIQUE constraint on
``webinar_video_jobs.zoom_recording_uuid``, which is the whole idempotency
guarantee, so the duplicate-webhook tests are testing the real mechanism rather
than a mock of it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401 - imports every model module, registering them with Base.metadata
from src.app_config.models import AppConfig
from src.config import settings
from src.db.base import Base
from src.workshops.models import Webinar, Workshop


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_as_text_on_sqlite(type_, compiler, **kw):
    return "TEXT"


# Seed tables the pipeline tests actually read and write. Everything these
# reference by foreign key is pulled in automatically by `_table_closure` —
# `webinars` alone reaches schools, cohorts, cycles and grade sets, and hand
# listing that graph goes stale the moment a column is added upstream.
PIPELINE_SEED_TABLES = (
    "webinar_video_jobs",
    "webinar_video_reels",
    "webinars",
    "workshops",
    # Written by the failure alert through emails.ses_client.send_email.
    "email_send_log",
    "email_suppression",
    "app_config",
    # Publishing runs Q&A answer extraction off the back of the notification.
    # Without these the extraction hits a missing table, and the swallow that
    # keeps a Bedrock outage from failing a published job would hide it.
    "webinar_qa_questions",
    "webinar_qa_answer_extractions",
    "webinar_qa_syncs",
)


def _table_closure(seed: tuple[str, ...]) -> set[str]:
    """Seed tables plus every table reachable from them through foreign keys.

    SQLite resolves each REFERENCES clause at CREATE TABLE time, so a partial
    subset fails with NoReferencedTableError rather than silently skipping the
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


ZOOM_WEBINAR_ID = "88812345678"


@pytest.fixture
def sessionmaker_factory():
    """Fresh in-memory SQLite engine + sessionmaker, scoped to one test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    wanted = _table_closure(PIPELINE_SEED_TABLES)
    test_metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name in wanted:
            table.to_metadata(test_metadata)
    test_metadata.tables["webinars"].c.duration_minutes.computed = None

    test_metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    # Sandbox off: the alert address is on the team domain either way, and the
    # tests stub the SES call rather than reaching the network.
    seed.add(AppConfig(email_sandbox_mode=False))
    seed.commit()
    seed.close()

    return SessionLocal


@pytest.fixture
def db(sessionmaker_factory):
    session = sessionmaker_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def webinar(db) -> Webinar:
    """One persisted webinar with a Zoom id for recordings to resolve against."""
    workshop = Workshop(id=uuid.uuid4(), name="Paying for College")
    row = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        webinar_name="Paying for College — Sept",
        zoom_webinar_id=ZOOM_WEBINAR_ID,
    )
    db.add_all([workshop, row])
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _no_cdn(monkeypatch):
    """Presigned S3 URLs unless a test sets a CDN, whatever the local .env says."""
    monkeypatch.setattr(settings, "cdn_base_url", "")
