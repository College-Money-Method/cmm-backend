"""A soft-deleted contact must leave no trace in the admin UI.

`School.contacts` is an unfiltered relationship, so `SchoolDetail` used to keep
listing contacts that every other endpoint had already dropped — both the ones an
admin deleted and the ones the Airtable sync offboarded. The eager loads in
`schools/router.py` now filter on `deleted_at`, which is what this pins down.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
import src.content.models  # noqa: F401 — register GradeSet/Cohort for FK metadata
import src.schools.models  # noqa: F401
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex: SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip.
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

LIVE_CONTACT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
# Deleted by an admin from the contacts UI (no Airtable record behind it).
DELETED_CONTACT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
# Deleted by the Airtable sync's offboarding sweep — same `deleted_at` flag.
OFFBOARDED_CONTACT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

DELETED_AT = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        t
        for n, t in Base.metadata.tables.items()
        if n in ("schools", "cohorts", "grade_sets", "contacts", "user_roles")
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=SCHOOL_ID, name="Acme High", slug="acme-high", is_current_customer=True))
    seed.add(
        Contact(
            id=LIVE_CONTACT_ID, school_id=SCHOOL_ID,
            email="live@example.com", first_name="Liv", last_name="Active",
        )
    )
    seed.add(
        Contact(
            id=DELETED_CONTACT_ID, school_id=SCHOOL_ID, deleted_at=DELETED_AT,
            email="deleted@example.com", first_name="Dana", last_name="Deleted",
        )
    )
    seed.add(
        Contact(
            id=OFFBOARDED_CONTACT_ID, school_id=SCHOOL_ID, deleted_at=DELETED_AT,
            airtable_id="recOffboarded1", email="offboarded@example.com",
            first_name="Ozzy", last_name="Offboarded",
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
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, role="super_admin", school_id=None
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_school_detail_lists_only_live_contacts(client: TestClient):
    resp = client.get(f"/api/v1/schools/{SCHOOL_ID}")
    assert resp.status_code == 200

    ids = {row["id"] for row in resp.json()["contacts"]}
    assert ids == {str(LIVE_CONTACT_ID)}


def test_school_detail_hides_airtable_offboarded_contacts(client: TestClient):
    """The sweep and the admin delete share `deleted_at`, so both must be hidden."""
    resp = client.get(f"/api/v1/schools/{SCHOOL_ID}")
    ids = {row["id"] for row in resp.json()["contacts"]}
    assert str(OFFBOARDED_CONTACT_ID) not in ids
    assert str(DELETED_CONTACT_ID) not in ids


def test_patch_school_response_also_hides_deleted_contacts(client: TestClient):
    """PATCH returns the same SchoolDetail — the filter can't live at one call site."""
    resp = client.patch(f"/api/v1/schools/{SCHOOL_ID}", json={"name": "Acme High School"})
    assert resp.status_code == 200

    ids = {row["id"] for row in resp.json()["contacts"]}
    assert ids == {str(LIVE_CONTACT_ID)}
