"""PATCH /app-config college_aid_sai_calculator_url: web URLs only, blank clears."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app

URL = "/api/v1/app-config"
FIELD = "college_aid_sai_calculator_url"


@pytest.fixture
def admin(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role="super_admin")
    yield TestClient(app)
    app.dependency_overrides.clear()


def _stored(db) -> str | None:
    db.expire_all()
    return db.query(AppConfig).one().college_aid_sai_calculator_url


def test_sets_trimmed_url_and_returns_it(admin, db):
    resp = admin.patch(URL, json={FIELD: "  https://studentaid.gov/aid-estimator/  "})
    assert resp.status_code == 200
    assert resp.json()[FIELD] == "https://studentaid.gov/aid-estimator/"
    assert _stored(db) == "https://studentaid.gov/aid-estimator/"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_clears(admin, db, blank):
    admin.patch(URL, json={FIELD: "https://example.org/sai"})
    assert admin.patch(URL, json={FIELD: blank}).status_code == 200
    assert _stored(db) is None


@pytest.mark.parametrize(
    "bad",
    ["javascript:alert(1)", "studentaid.gov", "ftp://example.org/x", "https://", "/college-aid", "https://x.org/" + "a" * 2000,
     "https://user:pw@x.org/", "https://x.org/a b"],
)
def test_rejects_non_web_urls(admin, db, bad):
    assert admin.patch(URL, json={FIELD: bad}).status_code == 422


def test_other_patches_leave_url_alone(admin, db):
    admin.patch(URL, json={FIELD: "https://example.org/sai"})
    admin.patch(URL, json={"survey_enabled": True})
    assert _stored(db) == "https://example.org/sai"
