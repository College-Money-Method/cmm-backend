"""A signed-in Hub user changing their own two email opt-ins from Preferences.

Same self-service endpoint as the display timezone, and scoped the same way: to
the contact row carrying the caller's own `user_id`. What differs is that the
opt-ins have to move the unsubscribe suppression with them, since a suppression
row blocks every send whatever the opt-ins say.

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
from src.emails.models import EmailSuppression
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
    teammate's row seeded opted-in at the same school."""

    def _build(role: str = "hub_user", auto: bool = False, broadcast: bool = False):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [
            t
            for n, t in Base.metadata.tables.items()
            if n in ("contacts", "schools", "user_roles", "email_suppression")
        ]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(Contact(
            id=SELF_CONTACT_ID, user_id=SELF_USER_ID, school_id=SCHOOL_ID,
            email="self@example.com", first_name="Self", last_name="One",
            auto_emails=auto, broadcast_emails=broadcast,
        ))
        seed.add(UserRole(user_id=SELF_USER_ID, role=role, school_id=SCHOOL_ID))
        seed.add(Contact(
            id=OTHER_CONTACT_ID, user_id=OTHER_USER_ID, school_id=SCHOOL_ID,
            email="other@example.com", first_name="Other", last_name="Two",
            auto_emails=True, broadcast_emails=True,
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


def _stored(client, contact_id=SELF_CONTACT_ID) -> tuple[bool, bool]:
    db = client._session_local()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        return contact.auto_emails, contact.broadcast_emails
    finally:
        db.close()


def _suppression(client, email: str = "self@example.com") -> EmailSuppression | None:
    db = client._session_local()
    try:
        return db.query(EmailSuppression).filter(EmailSuppression.email == email).first()
    finally:
        db.close()


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_any_hub_role_can_set_their_own_opt_ins(make_client, role: str):
    client = make_client(role)
    resp = client.patch(
        "/api/v1/auth/me/preferences",
        json={"auto_emails": True, "broadcast_emails": True},
    )

    assert resp.status_code == 200
    assert resp.json()["auto_emails"] is True
    assert resp.json()["broadcast_emails"] is True
    assert _stored(client) == (True, True)


def test_the_two_opt_ins_move_independently(make_client):
    client = make_client(auto=True, broadcast=True)
    client.patch("/api/v1/auth/me/preferences", json={"broadcast_emails": False})

    assert _stored(client) == (True, False)


def test_saving_a_zone_leaves_the_opt_ins_alone(make_client):
    """The timezone card posts only its own field, so an opt-in the user never
    touched must not be read as "off"."""
    client = make_client(auto=True, broadcast=True)
    client.patch("/api/v1/auth/me/preferences", json={"timezone": "America/Chicago"})

    assert _stored(client) == (True, True)


def test_turning_both_off_suppresses_every_send(make_client):
    client = make_client(auto=True, broadcast=True)
    client.patch(
        "/api/v1/auth/me/preferences",
        json={"auto_emails": False, "broadcast_emails": False},
    )

    row = _suppression(client)
    assert row is not None
    assert row.reason == "unsubscribe"


def test_opting_back_in_lifts_an_earlier_unsubscribe(make_client):
    """A suppression row blocks every send whatever the opt-ins say — re-opting
    in here has to lift it, or the Hub shows them subscribed to nothing."""
    client = make_client()
    db = client._session_local()
    db.add(EmailSuppression(email="self@example.com", reason="unsubscribe"))
    db.commit()
    db.close()

    client.patch("/api/v1/auth/me/preferences", json={"auto_emails": True})

    assert _suppression(client) is None


def test_opting_back_in_leaves_a_bounce_suppression_in_place(make_client):
    """A bounce is the receiving server's verdict; wanting mail cannot undo it."""
    client = make_client()
    db = client._session_local()
    db.add(EmailSuppression(email="self@example.com", reason="bounce"))
    db.commit()
    db.close()

    client.patch("/api/v1/auth/me/preferences", json={"auto_emails": True})

    row = _suppression(client)
    assert row is not None and row.reason == "bounce"


def test_setting_my_opt_ins_leaves_a_teammates_alone(make_client):
    client = make_client("hub_admin")
    client.patch(
        "/api/v1/auth/me/preferences",
        json={"auto_emails": False, "broadcast_emails": False},
    )

    assert _stored(client, OTHER_CONTACT_ID) == (True, True)


def test_me_reports_the_stored_opt_ins_back(make_client):
    client = make_client(auto=True)
    body = client.get("/api/v1/auth/me").json()

    assert body["auto_emails"] is True
    assert body["broadcast_emails"] is False


def test_an_account_with_no_contact_row_has_no_opt_ins_to_show(make_client):
    """super_admins are provisioned without a contact row: null, not False, so
    the Hub leaves the card out rather than showing them opted out of mail they
    were never going to be sent."""
    client = make_client()
    db = client._session_local()
    db.query(Contact).filter(Contact.id == SELF_CONTACT_ID).delete()
    db.commit()
    db.close()

    body = client.get("/api/v1/auth/me").json()
    assert body["auto_emails"] is None
    assert body["broadcast_emails"] is None
