"""A signed-in Hub user setting the timezone their own screens read in.

Reachable by every role: it decides nothing but what one person sees, and is
scoped to the contact row carrying their own `user_id` — there is no path here
to another person's row.

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
from src.schools.models import Contact

# Letter-only hex: SQLite's NUMERIC affinity corrupts digit-only UUID segments
# on these columns (see the sibling self-edit test for the full note).
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SELF_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SELF_CONTACT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
OTHER_USER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OTHER_CONTACT_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


@pytest.fixture
def make_client():
    """Factory: a TestClient acting as SELF_USER_ID with `role`, plus a
    teammate's row seeded at the same school to prove it stays untouched."""

    def _build(role: str = "hub_user", timezone: str | None = None):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [
            t
            for n, t in Base.metadata.tables.items()
            if n in ("contacts", "schools", "user_roles")
        ]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(Contact(
            id=SELF_CONTACT_ID, user_id=SELF_USER_ID, school_id=SCHOOL_ID,
            email="self@example.com", first_name="Self", last_name="One",
            timezone=timezone,
        ))
        seed.add(UserRole(user_id=SELF_USER_ID, role=role, school_id=SCHOOL_ID))
        seed.add(Contact(
            id=OTHER_CONTACT_ID, user_id=OTHER_USER_ID, school_id=SCHOOL_ID,
            email="other@example.com", first_name="Other", last_name="Two",
            timezone="America/Denver",
        ))
        seed.add(UserRole(user_id=OTHER_USER_ID, role="hub_user", school_id=SCHOOL_ID))
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
            user_id=SELF_USER_ID, role=role, school_id=SCHOOL_ID
        )
        client = TestClient(app)
        client._session_local = SessionLocal  # stashed for assertions
        return client

    yield _build
    app.dependency_overrides.clear()


def _stored(client, contact_id=SELF_CONTACT_ID) -> str | None:
    db = client._session_local()
    try:
        return db.query(Contact).filter(Contact.id == contact_id).first().timezone
    finally:
        db.close()


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_any_hub_role_can_set_their_own_zone(make_client, role: str):
    client = make_client(role)
    resp = client.patch("/api/v1/auth/me/preferences", json={"timezone": "America/Los_Angeles"})

    assert resp.status_code == 200
    assert resp.json()["timezone"] == "America/Los_Angeles"
    assert _stored(client) == "America/Los_Angeles"


def test_clearing_the_zone_means_use_my_browser(make_client):
    client = make_client(timezone="America/Los_Angeles")
    resp = client.patch("/api/v1/auth/me/preferences", json={"timezone": None})

    assert resp.status_code == 200
    assert resp.json()["timezone"] is None
    assert _stored(client) is None


def test_an_unsupported_zone_is_refused(make_client):
    client = make_client(timezone="America/Denver")
    resp = client.patch("/api/v1/auth/me/preferences", json={"timezone": "Mars/Olympus"})

    assert resp.status_code == 422
    # Refusal must leave the previous choice intact, not blank it.
    assert _stored(client) == "America/Denver"


def test_setting_my_zone_leaves_a_teammates_alone(make_client):
    """The write is keyed on the caller's own user_id, so there is no request
    shape that reaches someone else's row — including a school admin's."""
    client = make_client("hub_admin")
    client.patch("/api/v1/auth/me/preferences", json={"timezone": "Pacific/Honolulu"})

    assert _stored(client, OTHER_CONTACT_ID) == "America/Denver"


def test_me_reports_the_stored_zone_back(make_client):
    client = make_client(timezone="America/Chicago")
    assert client.get("/api/v1/auth/me").json()["timezone"] == "America/Chicago"


def test_an_account_with_no_contact_row_gets_a_clear_404(make_client):
    """super_admins are provisioned without a contact row; the request has
    nowhere to land, and a 500 would read as a server fault."""
    client = make_client()
    db = client._session_local()
    db.query(Contact).filter(Contact.id == SELF_CONTACT_ID).delete()
    db.commit()
    db.close()

    resp = client.patch("/api/v1/auth/me/preferences", json={"timezone": "America/Chicago"})
    assert resp.status_code == 404
