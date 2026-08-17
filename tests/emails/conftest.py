"""Shared in-memory SQLite fixture for pre-workshop scheduler tests.

Workshop/Webinar/School use Postgres-only column types (JSONB, TSVECTOR) and
one Postgres-only GENERATED column (``Webinar.duration_minutes``, built on
``EXTRACT(EPOCH FROM ...)``) that SQLite cannot compile. Registers dialect
compatibility shims for the types, and clones the ORM metadata into an
isolated copy (never mutating the shared ``Base.metadata`` used by real
Postgres migrations) with that one GENERATED clause stripped — it is not read
by any scheduler code path, so dropping it from the *test* schema is safe.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401 - imports every model module, registering them with Base.metadata
from src.app_config.models import AppConfig
from src.db.base import Base


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_as_text_on_sqlite(type_, compiler, **kw):
    return "TEXT"


# Only the tables the automation runner's query paths touch.
SCHEDULER_TEST_TABLES = (
    "schools",
    "cohorts",
    "grade_sets",
    "contacts",
    "workshops",
    "webinars",
    "cycles",
    "portal_mapping",
    "workshop_email_templates",
    "email_template",
    "email_automation",
    "automation_send_ledger",
    "email_send_log",
    "email_suppression",
    "broadcast",
    "content_assets",
    "workshop_resources",
    "asset_types",
    "user_roles",
    "app_config",
)


@pytest.fixture
def scheduler_sessionmaker():
    """Fresh in-memory SQLite engine + sessionmaker, scoped to one test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    test_metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name in SCHEDULER_TEST_TABLES:
            table.to_metadata(test_metadata)
    test_metadata.tables["webinars"].c.duration_minutes.computed = None

    test_metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    # Sandbox on for the test env: seeded recipients are @example.com (outside
    # the team domain), so the send pipeline withholds them and logs "sandboxed"
    # instead of calling SES — the network-safe default for scheduler tests.
    seed = SessionLocal()
    seed.add(AppConfig(email_sandbox_mode=True))
    seed.commit()
    seed.close()

    return SessionLocal
