"""Contact soft-delete vs hub-login revocation.

Two distinct admin actions on a contact, easy to conflate:

- POST /contacts/{id}/revoke-access — strips the hub login, KEEPS the contacts row
  (the person stays on the school's list and keeps receiving email).
- DELETE /contacts/{id} — soft-deletes the contact (`deleted_at`), cascading through
  login revocation first. Rejected for Airtable-managed rows, since Airtable owns
  those and the next sync would reactivate them (see schools/sync_contacts.py).

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
from src.auth.models import Profile, UserRole
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import Contact

# Letter-only hex: SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip (see the sibling test for the full repro).
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

# Admin-created contact with a hub login — the deletable case.
LOGIN_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
LOGIN_CONTACT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

# Admin-created contact with no hub login at all.
NO_LOGIN_CONTACT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

# Airtable-sourced contact — must not be deletable here.
AIRTABLE_CONTACT_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


class FakeSupabaseAdmin:
    """Records the auth users the endpoint asked Supabase to delete."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)


class FakeSupabase:
    def __init__(self) -> None:
        self.auth = type("_Auth", (), {"admin": FakeSupabaseAdmin()})()


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        t
        for n, t in Base.metadata.tables.items()
        if n in ("contacts", "schools", "user_roles", "profiles", "email_suppression")
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(
        Contact(
            id=LOGIN_CONTACT_ID, user_id=LOGIN_USER_ID, school_id=SCHOOL_ID,
            email="counselor@example.com", first_name="Casey", last_name="Login",
        )
    )
    seed.add(UserRole(user_id=LOGIN_USER_ID, role="hub_user", school_id=SCHOOL_ID))
    seed.add(Profile(user_id=LOGIN_USER_ID, email="counselor@example.com"))
    seed.add(
        Contact(
            id=NO_LOGIN_CONTACT_ID, school_id=SCHOOL_ID,
            email="nologin@example.com", first_name="Nora", last_name="Access",
        )
    )
    seed.add(
        Contact(
            id=AIRTABLE_CONTACT_ID, school_id=SCHOOL_ID, airtable_id="recAirtable123",
            email="synced@example.com", first_name="Sam", last_name="Synced",
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

    fake_supabase = FakeSupabase()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: fake_supabase
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, role="super_admin", school_id=None
    )
    c = TestClient(app)
    c._session_local = SessionLocal
    c._supabase = fake_supabase
    yield c
    app.dependency_overrides.clear()


def _contact(client, contact_id: uuid.UUID) -> Contact | None:
    db = client._session_local()
    try:
        return db.query(Contact).filter(Contact.id == contact_id).first()
    finally:
        db.close()


class TestSoftDelete:
    def test_delete_soft_deletes_a_login_less_contact(self, client):
        resp = client.delete(f"/api/v1/contacts/{NO_LOGIN_CONTACT_ID}")
        assert resp.status_code == 204

        contact = _contact(client, NO_LOGIN_CONTACT_ID)
        # Row survives, flagged deleted — every list and email audience filters on this.
        assert contact is not None
        assert contact.deleted_at is not None
        assert contact.is_active is False

    def test_delete_cascades_through_login_revocation(self, client):
        resp = client.delete(f"/api/v1/contacts/{LOGIN_CONTACT_ID}")
        assert resp.status_code == 204

        contact = _contact(client, LOGIN_CONTACT_ID)
        assert contact.deleted_at is not None
        # A hidden contact must not keep a working hub password.
        assert contact.user_id is None
        assert str(LOGIN_USER_ID) in client._supabase.auth.admin.deleted

        db = client._session_local()
        try:
            assert db.query(UserRole).filter(UserRole.user_id == LOGIN_USER_ID).first() is None
            assert db.query(Profile).filter(Profile.user_id == LOGIN_USER_ID).first() is None
        finally:
            db.close()

    def test_delete_rejects_airtable_managed_contact(self, client):
        resp = client.delete(f"/api/v1/contacts/{AIRTABLE_CONTACT_ID}")
        assert resp.status_code == 400
        assert "Airtable" in resp.json()["detail"]

        # Untouched: the sync would only reactivate it, so the row must stay live.
        contact = _contact(client, AIRTABLE_CONTACT_ID)
        assert contact.deleted_at is None

    def test_delete_hides_the_contact_from_the_list(self, client):
        assert client.delete(f"/api/v1/contacts/{NO_LOGIN_CONTACT_ID}").status_code == 204

        listed = client.get("/api/v1/contacts", params={"school_id": str(SCHOOL_ID)})
        assert listed.status_code == 200
        ids = {row["id"] for row in listed.json()["items"]}
        assert str(NO_LOGIN_CONTACT_ID) not in ids

    def test_delete_is_404_on_an_already_deleted_contact(self, client):
        assert client.delete(f"/api/v1/contacts/{NO_LOGIN_CONTACT_ID}").status_code == 204
        assert client.delete(f"/api/v1/contacts/{NO_LOGIN_CONTACT_ID}").status_code == 404


class TestRevokeAccess:
    def test_revoke_strips_the_login_but_keeps_the_contact(self, client):
        resp = client.post(f"/api/v1/contacts/{LOGIN_CONTACT_ID}/revoke-access")
        assert resp.status_code == 204

        contact = _contact(client, LOGIN_CONTACT_ID)
        # The person stays a contact — still listed, still emailable.
        assert contact.deleted_at is None
        assert contact.user_id is None
        assert str(LOGIN_USER_ID) in client._supabase.auth.admin.deleted

        db = client._session_local()
        try:
            assert db.query(UserRole).filter(UserRole.user_id == LOGIN_USER_ID).first() is None
        finally:
            db.close()

    def test_revoke_rejects_a_contact_with_no_login(self, client):
        resp = client.post(f"/api/v1/contacts/{NO_LOGIN_CONTACT_ID}/revoke-access")
        assert resp.status_code == 400
        assert "no hub login" in resp.json()["detail"]


class TestAirtableManagedFlag:
    def test_list_marks_airtable_and_admin_created_contacts(self, client):
        resp = client.get("/api/v1/contacts", params={"school_id": str(SCHOOL_ID)})
        assert resp.status_code == 200
        flags = {row["id"]: row["is_airtable_managed"] for row in resp.json()["items"]}
        # Drives whether the UI offers a delete control at all.
        assert flags[str(AIRTABLE_CONTACT_ID)] is True
        assert flags[str(NO_LOGIN_CONTACT_ID)] is False
        assert flags[str(LOGIN_CONTACT_ID)] is False
