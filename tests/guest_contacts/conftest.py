"""Shared harness for the guest-contacts endpoints.

In-memory SQLite + TestClient + dependency_overrides, following
tests/auth/test_me_timezone_preference.py. The session factory is stashed on the
client so a test can look at what actually landed in the table rather than
trusting the response it just read.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
from src.auth.deps import get_current_user
from src.auth.rate_limit import _hits
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def client():
    """A TestClient over an empty guest_contacts table, acting as super_admin."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [t for n, t in Base.metadata.tables.items() if n == "guest_contacts"]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_ID, role="super_admin", school_id=None
    )
    # The limiter is process-global, so each test starts from a clean window.
    _hits.clear()

    c = TestClient(app)
    c._session_local = SessionLocal  # stashed for assertions
    yield c

    app.dependency_overrides.clear()
    _hits.clear()
