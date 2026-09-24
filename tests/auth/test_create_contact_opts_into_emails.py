"""POST /contacts grants hub access, so it also subscribes the new counselor.

Mirrors the Airtable provisioning path (see
tests/schools/test_provisioning_opts_new_hub_users_into_emails.py): whichever
way hub access is granted, the person starts opted into both email streams
under CMM's opt-out policy, and an earlier unsubscribe is never overridden.

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
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.models import EmailSuppression
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex — SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip (see test_contact_auto_emails_self_edit).
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NEW_USER_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NEW_EMAIL = "newcounselor@example.com"


class _FakeUser:
    def __init__(self) -> None:
        self.id = str(NEW_USER_ID)
        self.email = NEW_EMAIL
        self.user_metadata = {"first_name": "New", "last_name": "Counselor"}


class _FakeAdmin:
    def create_user(self, params: dict):
        return type("_Resp", (), {"user": _FakeUser()})()


class _FakeSupabase:
    def __init__(self) -> None:
        self.auth = type("_Auth", (), {"admin": _FakeAdmin()})()


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    wanted = ("contacts", "schools", "user_roles", "profiles", "cohorts", "grade_sets", "email_suppression")
    tables = [t for n, t in Base.metadata.tables.items() if n in wanted]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=SCHOOL_ID, name="Hewitt Academy"))
    seed.commit()
    seed.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: _FakeSupabase()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, role="super_admin", school_id=None
    )
    test_client = TestClient(app)
    test_client._session_local = SessionLocal
    yield test_client
    app.dependency_overrides.clear()


def _create(client) -> None:
    resp = client.post(
        "/api/v1/contacts",
        json={"email": NEW_EMAIL, "role": "hub_user", "school_id": str(SCHOOL_ID)},
    )
    assert resp.status_code in (200, 201), resp.text


def test_new_hub_user_starts_subscribed_to_both_email_streams(client):
    _create(client)

    db = client._session_local()
    try:
        contact = db.query(Contact).filter(Contact.email == NEW_EMAIL).one()
        assert contact.auto_emails is True
        assert contact.broadcast_emails is True
    finally:
        db.close()


def test_an_earlier_unsubscribe_is_not_overridden_by_the_grant(client):
    db = client._session_local()
    try:
        db.add(EmailSuppression(email=NEW_EMAIL, reason="unsubscribe"))
        db.commit()
    finally:
        db.close()

    _create(client)

    db = client._session_local()
    try:
        contact = db.query(Contact).filter(Contact.email == NEW_EMAIL).one()
        assert contact.auto_emails is False
        assert contact.broadcast_emails is False
        assert db.query(EmailSuppression).count() == 1
    finally:
        db.close()
