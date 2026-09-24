"""Granting Counselor Hub access subscribes the person to CMM's emails.

CMM's stance is opt-out: a counselor handed hub access is there to run
workshops, so provisioning starts them subscribed to both the scheduler
automations and admin broadcasts rather than waiting for a checkbox almost
nobody finds. Only the *first* grant does this — a re-sync must never re-flip
an opt-in the counselor has since turned off — and an earlier unsubscribe is
never overridden.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# src.db.models imports every model module so the FKs on `schools` resolve
# when SQLite DDL is generated.
import src.db.models  # noqa: F401
from src.auth.models import UserRole
from src.db.base import Base
from src.emails.models import EmailSuppression
from src.schools import sync_provisioning
from src.schools.models import Contact, School


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    wanted = (
        "contacts", "schools", "user_roles", "profiles",
        "cohorts", "grade_sets", "email_suppression",
    )
    tables = [t for n, t in Base.metadata.tables.items() if n in wanted]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


class _FakeAdmin:
    def list_users(self, page: int, per_page: int) -> list:
        return []


class _FakeSupabase:
    """The seeded contacts are already linked to a user_id, so provisioning
    only reads the auth directory — create_user is never reached."""

    def __init__(self) -> None:
        self.auth = type("_Auth", (), {"admin": _FakeAdmin()})()


def _seed_contact(db, *, with_role: bool, opted_in: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    school = School(id=uuid.uuid4(), name="Hewitt Academy")
    db.add(school)
    user_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    db.add(Contact(
        id=contact_id,
        email="counselor@example.com",
        first_name="Casey",
        last_name="Counselor",
        role="Counselor",
        user_id=user_id,
        school_id=school.id,
        auto_emails=opted_in,
        broadcast_emails=opted_in,
    ))
    if with_role:
        db.add(UserRole(
            id=uuid.uuid4(),
            user_id=user_id,
            role="hub_user",
            school_id=school.id,
            school_role="Counselor",
        ))
    db.commit()
    return contact_id, user_id


def test_first_hub_grant_opts_the_counselor_into_both_email_streams(db_session, monkeypatch):
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    contact_id, _ = _seed_contact(db_session, with_role=False)

    result = sync_provisioning.provision_counselors_from_contacts(db_session, _FakeSupabase())

    assert result["counselors_created"] == 1
    contact = db_session.query(Contact).filter(Contact.id == contact_id).one()
    assert contact.auto_emails is True
    assert contact.broadcast_emails is True


def test_resync_does_not_reopen_an_opt_in_the_counselor_turned_off(db_session, monkeypatch):
    """Hub access already existed, so this run grants nothing — the defaults
    must not be re-applied over a deliberate opt-out."""
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    contact_id, _ = _seed_contact(db_session, with_role=True, opted_in=False)

    result = sync_provisioning.provision_counselors_from_contacts(db_session, _FakeSupabase())

    assert result["counselors_created"] == 0
    contact = db_session.query(Contact).filter(Contact.id == contact_id).one()
    assert contact.auto_emails is False
    assert contact.broadcast_emails is False


def test_regranting_access_does_not_resubscribe_a_partially_opted_out_contact(db_session, monkeypatch):
    """Revoking hub access leaves the contacts row and its opt-ins behind, so a
    later re-grant lands on somebody who has already chosen. Someone who kept
    broadcasts but turned workshop mail off must not have it switched back on.

    No unsubscribe suppression exists in this state — that row only appears when
    BOTH streams are off — so the suppression guard alone would miss this.
    """
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    contact_id, _ = _seed_contact(db_session, with_role=False)
    contact = db_session.query(Contact).filter(Contact.id == contact_id).one()
    contact.auto_emails = False
    contact.broadcast_emails = True
    db_session.commit()

    result = sync_provisioning.provision_counselors_from_contacts(db_session, _FakeSupabase())

    assert result["counselors_created"] == 1  # access granted again...
    contact = db_session.query(Contact).filter(Contact.id == contact_id).one()
    assert contact.auto_emails is False  # ...without reopening what they closed
    assert contact.broadcast_emails is True


def test_an_earlier_unsubscribe_survives_a_new_hub_grant(db_session, monkeypatch):
    """Being given hub access is not consent to re-subscribe someone who
    unsubscribed: flipping the opt-ins would also clear their suppression row
    on the next preference sync and silently undo the choice."""
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    contact_id, _ = _seed_contact(db_session, with_role=False)
    db_session.add(EmailSuppression(email="counselor@example.com", reason="unsubscribe"))
    db_session.commit()

    result = sync_provisioning.provision_counselors_from_contacts(db_session, _FakeSupabase())

    assert result["counselors_created"] == 1  # access is still granted...
    contact = db_session.query(Contact).filter(Contact.id == contact_id).one()
    assert contact.auto_emails is False  # ...but the mail stays off
    assert contact.broadcast_emails is False
    assert db_session.query(EmailSuppression).count() == 1
