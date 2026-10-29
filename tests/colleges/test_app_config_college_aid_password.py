"""PUT/DELETE /app-config/college-aid-password and what the public GET reveals."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.app_config.schemas import AppConfigOut
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.colleges.access_password import verify_password
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app

PASSWORD = "fake-pass-1234"
URL = "/api/v1/app-config/college-aid-password"


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


def test_put_sets_hashed_password_and_timestamp(admin, db):
    resp = admin.put(URL, json={"password": PASSWORD})
    assert resp.status_code == 204
    cfg = _cfg(db)
    assert cfg.college_aid_password_hash and PASSWORD not in cfg.college_aid_password_hash
    assert verify_password(PASSWORD, cfg.college_aid_password_hash)
    assert cfg.college_aid_password_updated_at is not None


def test_put_rotates_hash(admin, db):
    admin.put(URL, json={"password": PASSWORD})
    first = _cfg(db).college_aid_password_hash
    admin.put(URL, json={"password": "another-pass-5678"})
    assert _cfg(db).college_aid_password_hash != first


def test_put_rejects_short_password(admin, db):
    assert admin.put(URL, json={"password": "short"}).status_code == 422


def test_delete_clears_hash(admin, db):
    admin.put(URL, json={"password": PASSWORD})
    assert admin.delete(URL).status_code == 204
    assert _cfg(db).college_aid_password_hash is None


def test_non_admin_forbidden(viewer, db):
    assert viewer.put(URL, json={"password": PASSWORD}).status_code == 403
    assert viewer.delete(URL).status_code == 403


def test_public_get_exposes_flag_but_never_hash_or_plaintext(admin, db):
    before = admin.get("/api/v1/app-config").json()
    assert before["college_aid_password_set"] is False
    admin.put(URL, json={"password": PASSWORD})
    resp = admin.get("/api/v1/app-config")
    body = resp.json()
    assert body["college_aid_password_set"] is True
    assert body["college_aid_password_updated_at"]
    assert "college_aid_password_hash" not in body
    assert PASSWORD not in resp.text
    assert _cfg(db).college_aid_password_hash not in resp.text
    assert "college_aid_password_hash" not in AppConfigOut.model_json_schema()["properties"]


def test_generic_patch_cannot_write_hash(admin, db):
    admin.patch("/api/v1/app-config", json={"college_aid_password_hash": "attacker$value"})
    assert _cfg(db).college_aid_password_hash is None
