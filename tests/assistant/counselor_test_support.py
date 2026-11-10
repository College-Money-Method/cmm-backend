"""Schema and row helpers for the counselor assistant tests.

Workshop and school tables use Postgres-only types and one generated column, so
an isolated metadata copy is built for SQLite (the shared metadata is untouched).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401  (registers every model)
from src.db.base import Base
from src.workshops.models import PortalMapping, Webinar, Workshop

TABLES = (
    "app_config", "assistant_conversations", "assistant_messages",
    "schools", "cohorts", "grade_sets", "cycles", "workshops", "webinars", "portal_mapping",
    "workshop_registrations",
)


@compiles(JSONB, "sqlite")
def _jsonb(_t, _c, **_k):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _tsvector(_t, _c, **_k):
    return "TEXT"


def make_counselor_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    metadata = MetaData()
    for name in TABLES:
        Base.metadata.tables[name].to_metadata(metadata)
    metadata.tables["webinars"].c.duration_minutes.computed = None
    metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_webinar(db, school_id, start: datetime, *, name="FAFSA Night", grades="12", override=None, zoom="https://zoom.example/j/1"):
    """A workshop with one webinar mapped to ``school_id``; returns the webinar."""
    workshop = Workshop(id=uuid.uuid4(), name=name, suggested_grades=grades)
    webinar = Webinar(id=uuid.uuid4(), workshop_id=workshop.id, start_datetime=start, registration_url=zoom)
    db.add_all([workshop, webinar])
    db.flush()
    if school_id is not None:
        db.add(PortalMapping(school_id=school_id, webinar_id=webinar.id, school_override=override))
    db.commit()
    return webinar
