"""SQLite-backed app fixture shared by the preview/session tests.

Postgres-only column types are compiled to SQLite equivalents so the real
content, workshop and school tables can be created in memory.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.guest_contacts.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.workshops.models  # noqa: F401
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app


@compiles(JSONB, "sqlite")
def _jsonb(_t, _c, **_k):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array(_t, _c, **_k):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _tsvector(_t, _c, **_k):
    return "TEXT"


def make_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in Base.metadata.sorted_tables:
        try:
            table.create(engine)
        except Exception:  # noqa: BLE001 - webinars uses Postgres-only DDL; real-webinar paths are not exercised here
            continue
    return sessionmaker(bind=engine, expire_on_commit=False)


def install(factory) -> TestClient:
    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    return TestClient(app)


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)
