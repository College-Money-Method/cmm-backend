"""Calculators API: validation, slug conflicts, and the draft/published gate.

The suite runs on in-memory SQLite. ``config`` and ``embed_allowed_origins`` are
JSONB, which SQLite cannot compile, so a dialect shim maps JSONB to JSON — the
same approach as tests/emails/conftest.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.auth.deps import require_admin
from src.auth.schemas import CurrentUser
from src.calculators.models import Calculator
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app

PUBLISHED_SLUG = "fafsa-sai-2027-28"
DRAFT_SLUG = "student-borrowing-8-percent"
BASE = "/api/v1/calculators"

PUBLISHED_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DRAFT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def client() -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["calculators"]])
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(
        Calculator(
            id=PUBLISHED_ID,
            slug=PUBLISHED_SLUG,
            title="FAFSA 2027-28 SAI Calculator",
            type="fafsa_sai",
            html="<div>sai</div>",
            config={"award_year": "2027-28", "sai_floor": -1500},
            documentation="# SAI\n\nThe workbook is wrong about Medicare.",
            status="published",
        )
    )
    seed.add(
        Calculator(
            id=DRAFT_ID,
            slug=DRAFT_SLUG,
            title="Student Borrowing: the 8% Rule",
            type="student_borrowing_8_percent",
            status="draft",
        )
    )
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
    app.dependency_overrides[require_admin] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", school_id=None, school_role=None,
        email="admin@example.com",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Public endpoints ──────────────────────────────────────────────────────────

def test_public_get_published_returns_html_and_config(client):
    r = client.get(f"{BASE}/public/{PUBLISHED_SLUG}")
    assert r.status_code == 200
    body = r.json()
    assert body["html"] == "<div>sai</div>"
    assert body["config"]["sai_floor"] == -1500
    assert body["embed_allowed_origins"] == []


def test_public_get_omits_authoring_documentation(client):
    """Internal authoring notes must not ride along on the unauthenticated route."""
    body = client.get(f"{BASE}/public/{PUBLISHED_SLUG}").json()
    assert body["documentation"] is None


def test_public_get_draft_is_404(client):
    """A draft can hold a half-finished formula — slugs are guessable."""
    assert client.get(f"{BASE}/public/{DRAFT_SLUG}").status_code == 404


def test_public_slug_list_excludes_drafts(client):
    r = client.get(f"{BASE}/public")
    assert r.status_code == 200
    assert [c["slug"] for c in r.json()] == [PUBLISHED_SLUG]


def test_public_route_not_parsed_as_uuid(client):
    """/public must be matched before /{calculator_id}, not as a UUID."""
    assert client.get(f"{BASE}/public").status_code == 200


# ── Admin endpoints ───────────────────────────────────────────────────────────

def test_list_returns_all_with_total(client):
    r = client.get(BASE)
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_create_returns_201_and_defaults(client):
    r = client.post(BASE, json={"slug": "net-worth", "title": "Net Worth", "type": "business_net_worth"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft"
    assert body["config"] == {}
    assert body["embed_allowed_origins"] == []


def test_create_duplicate_slug_is_409(client):
    r = client.post(BASE, json={"slug": PUBLISHED_SLUG, "title": "Dupe", "type": "fafsa_sai"})
    assert r.status_code == 409


def test_create_invalid_type_is_422(client):
    r = client.post(BASE, json={"slug": "x", "title": "X", "type": "not_a_calculator"})
    assert r.status_code == 422


def test_create_invalid_status_is_422(client):
    r = client.post(BASE, json={"slug": "x", "title": "X", "type": "fafsa_sai", "status": "live"})
    assert r.status_code == 422


def test_patch_sets_updated_at_and_persists_config(client):
    assert client.get(f"{BASE}/{PUBLISHED_ID}").json()["updated_at"] is None
    r = client.patch(f"{BASE}/{PUBLISHED_ID}", json={"config": {"max_pell_award": 7395}})
    assert r.status_code == 200
    body = r.json()
    assert body["updated_at"] is not None
    assert body["config"] == {"max_pell_award": 7395}


def test_admin_get_returns_authoring_documentation(client):
    """The editor's Documentation tab reads it from here."""
    body = client.get(f"{BASE}/{PUBLISHED_ID}").json()
    assert body["documentation"].startswith("# SAI")


def test_patch_persists_authoring_documentation(client):
    brief = "## Data\n\nEvery figure comes from window.__CALC_CONFIG__."
    r = client.patch(f"{BASE}/{DRAFT_ID}", json={"documentation": brief})
    assert r.status_code == 200
    assert r.json()["documentation"] == brief
    assert client.get(f"{BASE}/{DRAFT_ID}").json()["documentation"] == brief


def test_patch_to_taken_slug_is_409(client):
    r = client.patch(f"{BASE}/{DRAFT_ID}", json={"slug": PUBLISHED_SLUG})
    assert r.status_code == 409


def test_patch_same_slug_is_allowed(client):
    """Re-sending the row's own slug must not self-conflict."""
    r = client.patch(f"{BASE}/{PUBLISHED_ID}", json={"slug": PUBLISHED_SLUG, "title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"


def test_publishing_a_draft_makes_it_publicly_reachable(client):
    assert client.get(f"{BASE}/public/{DRAFT_SLUG}").status_code == 404
    assert client.patch(f"{BASE}/{DRAFT_ID}", json={"status": "published"}).status_code == 200
    assert client.get(f"{BASE}/public/{DRAFT_SLUG}").status_code == 200


def test_get_unknown_id_is_404(client):
    assert client.get(f"{BASE}/{uuid.uuid4()}").status_code == 404


def test_delete_removes_row(client):
    assert client.delete(f"{BASE}/{DRAFT_ID}").status_code == 204
    assert client.get(f"{BASE}/{DRAFT_ID}").status_code == 404


def test_admin_endpoints_require_auth():
    """Without the dependency override, admin routes reject anonymous callers."""
    app.dependency_overrides.clear()
    with TestClient(app) as anon:
        assert anon.get(BASE).status_code in (401, 403)
