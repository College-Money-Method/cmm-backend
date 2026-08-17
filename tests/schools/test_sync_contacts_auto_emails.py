"""Airtable contact sync must never touch `auto_emails` — Phase 2 makes it a
counselor self-service opt-in (see auth/router.py update_contact); Airtable's
"Auto Emails" field is no longer read into `new_values` on either the create
or update path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.base import Base
from src.schools import sync_contacts
from src.schools.models import Contact


def _airtable_record(rec_id: str, email: str, auto_emails: bool) -> dict:
    return {
        "id": rec_id,
        "fields": {
            "Email": email,
            "First Name": "Test",
            "Last Name": "Counselor",
            # If sync_contacts still read this, it would flip auto_emails on
            # every run — exactly what Phase 2 must prevent.
            "Auto Emails": auto_emails,
        },
    }


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [t for n, t in Base.metadata.tables.items() if n in ("contacts", "schools")]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


def test_new_contact_ignores_airtable_auto_emails_true(db_session, monkeypatch):
    """A brand-new contact must land with the model default (False), never the
    Airtable-supplied True — proves `auto_emails` isn't in `new_values` at all
    (a present-but-ignored key would still coincidentally default correctly on
    create; the update-path test below is the one that actually distinguishes
    "excluded from new_values" from "read but recomputed the same way")."""
    monkeypatch.setattr(
        sync_contacts, "get_contacts_records",
        lambda: [_airtable_record("rec1", "new@example.com", auto_emails=True)],
    )

    result = sync_contacts.sync_contacts_from_airtable(db_session)

    assert result["contacts_created"] == 1
    contact = db_session.query(Contact).filter(Contact.email == "new@example.com").first()
    assert contact.auto_emails is False


def test_existing_counselor_opt_in_survives_airtable_resync(db_session, monkeypatch):
    """A counselor who self-opted-in (auto_emails=True) must keep that value
    across a re-sync even when Airtable's "Auto Emails" field says False —
    this is the regression Phase 2 exists to prevent."""
    existing = Contact(
        airtable_id="rec1", email="counselor@example.com", first_name="Old", last_name="Name",
        auto_emails=True,
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(
        sync_contacts, "get_contacts_records",
        lambda: [_airtable_record("rec1", "counselor@example.com", auto_emails=False)],
    )

    result = sync_contacts.sync_contacts_from_airtable(db_session)

    assert result["contacts_updated"] == 1  # name fields changed, proving the row WAS processed
    contact = db_session.query(Contact).filter(Contact.email == "counselor@example.com").first()
    assert contact.auto_emails is True
