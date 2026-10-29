"""Shared harness for the colleges tests: in-memory SQLite with the college tables.

The admin ``client`` mounts the admin routers on a fresh FastAPI app (no real
auth/Supabase); ``acting_as`` switches the signed-in role. Each request gets its own
session on the test engine, like production, while ``db`` stays available to seed and
inspect. Modules that need another app (``test_public_router``) define their own ``client``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401  (registers every model so relationship strings resolve)
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.colleges import admin_edit_router, admin_router
from src.colleges.models import College
from src.colleges.name_key import normalize_name
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from tests.colleges.admin_helpers import ADMIN_ID

TABLES = ("colleges", "college_aid_records", "app_config")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[n] for n in TABLES])
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def college(db) -> College:
    row = College(
        name="Adelphi University", name_key=normalize_name("Adelphi University"),
        state="NY", region="Mid-Atlantic", control="private",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def acting_as():
    """Mutable current user; tests set ``acting_as.user`` to another role."""
    class Holder:
        user = CurrentUser(user_id=ADMIN_ID, role="super_admin", school_id=None)

    return Holder


@pytest.fixture
def client(db, acting_as):
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(admin_router.router)
    app.include_router(admin_edit_router.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: acting_as.user
    return TestClient(app)
