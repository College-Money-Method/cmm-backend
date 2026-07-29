"""Public (no-auth) school endpoints must hide non-customer schools.

Regression: prospect schools (is_current_customer=False) were reachable by
direct URL (e.g. /school/boston-university-academy). Public portal routes now
return the same 404 as a missing school so prospects can't be reached.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.content.models  # noqa: F401 — register GradeSet/Cohort for FK metadata
import src.schools.models  # noqa: F401
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import School

CUSTOMER_SLUG = "acme-high"
PROSPECT_SLUG = "boston-university-academy"
# Prospect an admin has activated the SRC for (preview-link access)
ACTIVATED_PROSPECT_SLUG = "riverside-prep"
NOT_FOUND_DETAIL = "We couldn't find your partnered school, contact College Money Method"

CUSTOMER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROSPECT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ACTIVATED_PROSPECT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def client() -> TestClient:
    """TestClient backed by an in-memory SQLite DB seeded with two schools."""
    # StaticPool + single shared connection keeps the in-memory DB alive across
    # sessions (each new connection would otherwise get an empty database).
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [t for n, t in Base.metadata.tables.items() if n in ("schools", "cohorts", "grade_sets")]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=CUSTOMER_ID, name="Acme High", slug=CUSTOMER_SLUG, is_current_customer=True))
    seed.add(School(id=PROSPECT_ID, name="Boston University Academy", slug=PROSPECT_SLUG, is_current_customer=False))
    seed.add(School(
        id=ACTIVATED_PROSPECT_ID, name="Riverside Prep", slug=ACTIVATED_PROSPECT_SLUG,
        is_current_customer=False, is_cmm_website_activated=True,
    ))
    seed.commit()
    seed.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_customer_school_reachable_by_slug(client: TestClient):
    resp = client.get(f"/api/v1/schools/slug/{CUSTOMER_SLUG}")
    assert resp.status_code == 200
    assert resp.json()["slug"] == CUSTOMER_SLUG


def test_prospect_school_hidden_by_slug(client: TestClient):
    resp = client.get(f"/api/v1/schools/slug/{PROSPECT_SLUG}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == NOT_FOUND_DETAIL


def test_prospect_school_hidden_by_id_public(client: TestClient):
    resp = client.get(f"/api/v1/schools/{PROSPECT_ID}/public")
    assert resp.status_code == 404
    assert resp.json()["detail"] == NOT_FOUND_DETAIL


def test_prospect_school_counselors_hidden(client: TestClient):
    resp = client.get(f"/api/v1/schools/slug/{PROSPECT_SLUG}/counselors")
    assert resp.status_code == 404
    assert resp.json()["detail"] == NOT_FOUND_DETAIL


def test_prospect_school_verify_password_hidden(client: TestClient):
    # 404 before any password check — never reveals the prospect exists
    resp = client.post(f"/api/v1/schools/slug/{PROSPECT_SLUG}/verify-password", json={"password": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == NOT_FOUND_DETAIL


def test_missing_school_same_404(client: TestClient):
    resp = client.get("/api/v1/schools/slug/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == NOT_FOUND_DETAIL


def test_activated_prospect_reachable_by_slug(client: TestClient):
    resp = client.get(f"/api/v1/schools/slug/{ACTIVATED_PROSPECT_SLUG}")
    assert resp.status_code == 200
    assert resp.json()["slug"] == ACTIVATED_PROSPECT_SLUG


def test_activated_prospect_reachable_by_id_public(client: TestClient):
    resp = client.get(f"/api/v1/schools/{ACTIVATED_PROSPECT_ID}/public")
    assert resp.status_code == 200


def test_activated_prospect_verify_password_passes_gate(client: TestClient):
    # Past the visibility gate: wrong password is 401 (not the 404 a hidden
    # school returns), proving the SRC is reachable for an activated prospect.
    resp = client.post(
        f"/api/v1/schools/slug/{ACTIVATED_PROSPECT_SLUG}/verify-password",
        json={"password": "wrong"},
    )
    assert resp.status_code == 401


def test_activated_prospect_absent_from_public_directory(client: TestClient):
    # Activated prospects get a private preview link but are NOT advertised in
    # the public discovery list — that stays current-customers only.
    resp = client.get("/api/v1/schools/public")
    assert resp.status_code == 200
    slugs = {item["slug"] for item in resp.json()["items"]}
    assert CUSTOMER_SLUG in slugs
    assert ACTIVATED_PROSPECT_SLUG not in slugs
