"""Contact self-edit authz (Phase 2 opt-in): a hub user of ANY role may toggle
their own `auto_emails`, but a non-admin can never change any role (including
their own), and self-edit never unlocks fields beyond what the existing
role-based branches already allow for that user.

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/schools/test_public_school_access.py.
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

# Letter-only hex (no digit-only segments): SQLite applies NUMERIC column
# affinity to postgresql.UUID(as_uuid=True) columns (UserRole.user_id/
# school_id), and silently coerces an all-digit hex string (e.g.
# "66666666-6666-...") to a float on write, corrupting the round trip back to
# uuid.UUID on read. Confirmed via isolated repro: random uuid4() values and
# these letter-only constants both round-trip correctly; repeated-digit
# constants do not.
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

SELF_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SELF_CONTACT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

OTHER_USER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OTHER_CONTACT_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


@pytest.fixture
def make_client():
    """Factory: build a TestClient acting as `role` (SELF_USER_ID), with SELF and
    OTHER contacts seeded at the same school, each with a matching UserRole."""

    def _build(role: str):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [t for n, t in Base.metadata.tables.items() if n in ("contacts", "schools", "user_roles")]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(Contact(
            id=SELF_CONTACT_ID, user_id=SELF_USER_ID, school_id=SCHOOL_ID,
            email="self@example.com", first_name="Self", last_name="One", auto_emails=False,
        ))
        seed.add(UserRole(user_id=SELF_USER_ID, role=role, school_id=SCHOOL_ID))
        seed.add(Contact(
            id=OTHER_CONTACT_ID, user_id=OTHER_USER_ID, school_id=SCHOOL_ID,
            email="other@example.com", first_name="Other", last_name="Two", auto_emails=False,
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


@pytest.mark.parametrize("role", ["hub_user", "viewer"])
def test_non_admin_can_toggle_own_auto_emails(make_client, role: str):
    client = make_client(role)
    resp = client.patch(f"/api/v1/contacts/{SELF_CONTACT_ID}", json={"auto_emails": True})
    assert resp.status_code == 200
    assert resp.json()["auto_emails"] is True

    db = client._session_local()
    try:
        contact = db.query(Contact).filter(Contact.id == SELF_CONTACT_ID).first()
        assert contact.auto_emails is True
    finally:
        db.close()


@pytest.mark.parametrize("role", ["hub_user", "viewer"])
def test_non_admin_cannot_edit_others_row(make_client, role: str):
    client = make_client(role)
    resp = client.patch(f"/api/v1/contacts/{OTHER_CONTACT_ID}", json={"auto_emails": True})
    assert resp.status_code == 200  # title-only branch: silently no-ops non-title fields

    db = client._session_local()
    try:
        other = db.query(Contact).filter(Contact.id == OTHER_CONTACT_ID).first()
        # auto_emails self-edit is scoped to `user.user_id == contact.user_id` —
        # editing a teammate's row must never flip their auto_emails.
        assert other.auto_emails is False
    finally:
        db.close()


@pytest.mark.parametrize("role", ["hub_user", "viewer"])
def test_non_admin_self_edit_with_role_field_is_rejected(make_client, role: str):
    client = make_client(role)
    resp = client.patch(
        f"/api/v1/contacts/{SELF_CONTACT_ID}", json={"auto_emails": True, "role": "hub_admin"}
    )
    assert resp.status_code == 403

    db = client._session_local()
    try:
        role_record = db.query(UserRole).filter(UserRole.user_id == SELF_USER_ID).first()
        contact = db.query(Contact).filter(Contact.id == SELF_CONTACT_ID).first()
        # Rejected outright — not even the (otherwise legal) auto_emails change applies.
        assert role_record.role == role
        assert contact.auto_emails is False
    finally:
        db.close()


@pytest.mark.parametrize("role", ["hub_user", "viewer"])
def test_non_admin_self_edit_does_not_unlock_name_fields(make_client, role: str):
    client = make_client(role)
    resp = client.patch(
        f"/api/v1/contacts/{SELF_CONTACT_ID}",
        json={"auto_emails": True, "first_name": "Hacked"},
    )
    assert resp.status_code == 200

    db = client._session_local()
    try:
        contact = db.query(Contact).filter(Contact.id == SELF_CONTACT_ID).first()
        # auto_emails applies (self-edit), but first_name stays untouched — the
        # existing non-hub_admin branch remains title-only for name/role fields.
        assert contact.auto_emails is True
        assert contact.first_name == "Self"
    finally:
        db.close()


def test_hub_admin_self_role_change_still_works(make_client):
    """Regression: hub_admin's existing ability to change their own hub
    permission must survive the new non-admin role guard (which only blocks
    hub_user/viewer)."""
    client = make_client("hub_admin")
    resp = client.patch(f"/api/v1/contacts/{SELF_CONTACT_ID}", json={"role": "hub_user"})
    assert resp.status_code == 200

    db = client._session_local()
    try:
        role_record = db.query(UserRole).filter(UserRole.user_id == SELF_USER_ID).first()
        assert role_record.role == "hub_user"
    finally:
        db.close()
