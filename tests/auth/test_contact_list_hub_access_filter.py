"""GET /contacts?hub_access_only=true — the email compose recipient picker.

The picker must offer only contacts the broadcast can actually reach, or an
admin adds someone the send path then silently drops (see emails.hub_access).

Also a compile guard: `list_contacts` outer-joins `user_roles` into the OUTER
query, which is exactly where the correlated EXISTS predicate can auto-correlate
itself into a FROM-less subquery SQLAlchemy refuses to compile.

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/auth/test_contact_auto_emails_self_edit.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
import src.schools.models  # noqa: F401
from src.auth.deps import get_current_user
from src.auth.models import UserRole
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex — SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip (see test_contact_auto_emails_self_edit).
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
HUB_USER_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    wanted = ("contacts", "schools", "user_roles", "profiles", "cohorts", "grade_sets")
    tables = [t for n, t in Base.metadata.tables.items() if n in wanted]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=SCHOOL_ID, name="Hewitt Academy", is_current_customer=True))
    seed.add(Contact(
        id=uuid.uuid4(), user_id=HUB_USER_ID, school_id=SCHOOL_ID,
        email="counselor@example.com", first_name="Casey", last_name="Counselor",
    ))
    seed.add(UserRole(user_id=HUB_USER_ID, school_id=SCHOOL_ID, role="hub_user"))
    # Same school, on the directory, never given a hub login.
    seed.add(Contact(
        id=uuid.uuid4(), school_id=SCHOOL_ID,
        email="frontdesk@example.com", first_name="Dana", last_name="Desk",
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
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, role="super_admin", school_id=None
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_hub_access_only_hides_the_contact_with_no_login(client):
    resp = client.get("/api/v1/contacts", params={"hub_access_only": "true"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["email"] for row in body["items"]] == ["counselor@example.com"]
    assert body["total"] == 1


def test_the_default_listing_still_shows_every_contact(client):
    """The admin contact directory is not an email audience — leaving the flag
    off must keep showing login-less staff."""
    resp = client.get("/api/v1/contacts")

    assert resp.status_code == 200, resp.text
    emails = {row["email"] for row in resp.json()["items"]}
    assert emails == {"counselor@example.com", "frontdesk@example.com"}
