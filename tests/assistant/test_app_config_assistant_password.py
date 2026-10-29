"""PUT/DELETE /app-config/assistant-password and the derived flag."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.colleges.access_password import verify_password
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app

PASSWORD = "fake-pass-1234"
URL = "/api/v1/app-config/assistant-password"


def _client(db, role):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role=role)
    return TestClient(app)


@pytest.fixture
def admin(db):
    yield _client(db, "super_admin")
    app.dependency_overrides.clear()


@pytest.fixture
def viewer(db):
    yield _client(db, "viewer")
    app.dependency_overrides.clear()


def _cfg(db) -> AppConfig:
    db.expire_all()
    return db.query(AppConfig).one()


def test_put_sets_hash_and_flag(admin, db):
    assert admin.put(URL, json={"password": PASSWORD}).status_code == 204
    cfg = _cfg(db)
    assert verify_password(PASSWORD, cfg.assistant_password_hash)
    assert cfg.assistant_password_updated_at is not None
    assert cfg.college_aid_password_hash is None
    body = admin.get("/api/v1/app-config").json()
    assert body["assistant_password_set"] is True
    assert "assistant_password_hash" not in body


def test_delete_clears(admin, db):
    admin.put(URL, json={"password": PASSWORD})
    assert admin.delete(URL).status_code == 204
    assert _cfg(db).assistant_password_hash is None


def test_rejects_short_password(admin):
    assert admin.put(URL, json={"password": "short"}).status_code == 422


def test_non_admin_forbidden(viewer):
    assert viewer.put(URL, json={"password": PASSWORD}).status_code == 403
