"""Admin-editable school slug: validation, duplicate rejection and stable URLs.

Regression this covers: creating a second school with an already-used name gave
it a `-2` slug with no way to correct it, because `slug` was auto-derived from
the name and not exposed on the create/update payloads.

Also pins the ownership rule established by migration 0108 — `slug` is the
authoritative public URL segment and is no longer shadowed by `airtable_slug`.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.content.models  # noqa: F401 — register GradeSet/Cohort for FK metadata
import src.schools.models  # noqa: F401
from src.auth.deps import CurrentUser, require_admin, get_current_user
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import School

BASE = "/api/v1/schools"

# Mirrors the reported situation: the original record was renamed, and a second
# school then took the original name and got a "-2" slug.
RENAMED_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
SECOND_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
# A school whose public URL comes from a legacy Airtable slug.
LEGACY_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")

ADMIN = CurrentUser(
    user_id=uuid.uuid4(), role="super_admin", school_id=None, school_role=None,
    email="admin@example.com",
)


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        t for n, t in Base.metadata.tables.items()
        if n in ("schools", "cohorts", "grade_sets", "contacts")
    ]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(db_factory) -> TestClient:
    seed = db_factory()
    seed.add(School(
        id=RENAMED_ID, name="The Dwight School New York",
        slug="dwight-global-online-school", is_current_customer=True,
    ))
    seed.add(School(
        id=SECOND_ID, name="Dwight Global Online School",
        slug="dwight-global-online-school-2", is_current_customer=True,
    ))
    seed.add(School(
        id=LEGACY_ID, name="Ursuline Academy of Dallas",
        slug="ursuline-academy-of-dallas", airtable_slug="ursuline-academy-of-dallas",
        is_current_customer=True,
    ))
    seed.commit()
    seed.close()

    def override_get_db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Duplicate check endpoint (backs the admin form's live validation) ──────────

def test_slug_available_reports_free_slug(client):
    r = client.get(f"{BASE}/slug-available", params={"slug": "the-dwight-school-new-york"})
    assert r.status_code == 200
    assert r.json() == {
        "available": True, "slug": "the-dwight-school-new-york", "reason": None
    }


def test_slug_available_reports_taken_slug_with_owner(client):
    r = client.get(f"{BASE}/slug-available", params={"slug": "dwight-global-online-school"})
    assert r.json()["available"] is False
    assert "The Dwight School New York" in r.json()["reason"]


def test_slug_available_excludes_the_school_being_edited(client):
    r = client.get(
        f"{BASE}/slug-available",
        params={"slug": "dwight-global-online-school", "exclude_id": str(RENAMED_ID)},
    )
    assert r.json()["available"] is True


def test_slug_available_normalizes_input(client):
    r = client.get(f"{BASE}/slug-available", params={"slug": "  The Dwight School, NY! "})
    assert r.json() == {"available": True, "slug": "the-dwight-school-ny", "reason": None}


def test_slug_available_rejects_reserved_slug(client):
    r = client.get(f"{BASE}/slug-available", params={"slug": "school"})
    assert r.json()["available"] is False
    assert "reserved" in r.json()["reason"]


def test_slug_available_matches_legacy_airtable_slug(client):
    """A legacy Airtable slug still resolves publicly, so it can't be reused."""
    r = client.get(f"{BASE}/slug-available", params={"slug": "ursuline-academy-of-dallas"})
    assert r.json()["available"] is False


# ── Update ────────────────────────────────────────────────────────────────────

def test_update_sets_custom_slug(client):
    r = client.patch(f"{BASE}/{RENAMED_ID}", json={"slug": "the-dwight-school-new-york"})
    assert r.status_code == 200
    assert r.json()["slug"] == "the-dwight-school-new-york"


def test_update_rejects_duplicate_slug(client):
    r = client.patch(f"{BASE}/{SECOND_ID}", json={"slug": "dwight-global-online-school"})
    assert r.status_code == 409
    assert "already used by The Dwight School New York" in r.json()["detail"]


def test_update_allows_reusing_a_freed_slug(client):
    """The reported swap: free the slug on one school, claim it on the other."""
    assert client.patch(
        f"{BASE}/{RENAMED_ID}", json={"slug": "the-dwight-school-new-york"}
    ).status_code == 200
    r = client.patch(f"{BASE}/{SECOND_ID}", json={"slug": "dwight-global-online-school"})
    assert r.status_code == 200
    assert r.json()["slug"] == "dwight-global-online-school"


def test_update_keeping_own_slug_is_not_a_duplicate(client):
    r = client.patch(f"{BASE}/{RENAMED_ID}", json={"slug": "dwight-global-online-school"})
    assert r.status_code == 200


def test_update_rejects_reserved_slug(client):
    r = client.patch(f"{BASE}/{RENAMED_ID}", json={"slug": "admin"})
    assert r.status_code == 400


def test_update_rejects_empty_after_normalization(client):
    r = client.patch(f"{BASE}/{RENAMED_ID}", json={"slug": "!!!"})
    assert r.status_code == 400


def test_update_blank_slug_regenerates_from_name(client):
    r = client.patch(f"{BASE}/{SECOND_ID}", json={"slug": ""})
    assert r.status_code == 200
    # The original name's slug is held by the other school, so it falls back to -2
    assert r.json()["slug"] == "dwight-global-online-school-2"


def test_update_without_slug_key_leaves_it_untouched(client):
    r = client.patch(f"{BASE}/{RENAMED_ID}", json={"nickname": "Dwight NY Families"})
    assert r.status_code == 200
    assert r.json()["slug"] == "dwight-global-online-school"


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_with_custom_slug(client):
    r = client.post(BASE, json={"name": "Riverside Prep", "slug": "riverside"})
    assert r.status_code == 201
    assert r.json()["slug"] == "riverside"


def test_create_without_slug_derives_from_name(client):
    r = client.post(BASE, json={"name": "Riverside Prep"})
    assert r.status_code == 201
    assert r.json()["slug"] == "riverside-prep"


def test_create_rejects_duplicate_slug(client):
    r = client.post(BASE, json={"name": "Other", "slug": "dwight-global-online-school"})
    assert r.status_code == 409


# ── slug is authoritative over airtable_slug (migration 0108) ─────────────────

def test_public_response_uses_slug_not_airtable_slug(client, db_factory):
    """A custom slug must win; airtable_slug no longer shadows it."""
    session = db_factory()
    session.query(School).filter(School.id == LEGACY_ID).update(
        {"slug": "ursuline-dallas"}
    )
    session.commit()
    session.close()

    r = client.get(f"{BASE}/slug/ursuline-dallas")
    assert r.status_code == 200
    assert r.json()["slug"] == "ursuline-dallas"

    # The old Airtable slug still resolves, but reports the current slug
    r = client.get(f"{BASE}/slug/ursuline-academy-of-dallas")
    assert r.status_code == 200
    assert r.json()["slug"] == "ursuline-dallas"
