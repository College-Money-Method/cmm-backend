"""The one-time backfill of pre-guard rows, and the re-run it has to survive.

The script exists to clear junk that was already in the table when the guard
shipped. Because someone will inevitably run it twice, the property that matters
is that a second pass leaves every admin decision standing — in particular a row
an admin rescued from the Spam tab, which must not quietly go back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.backfill import backfill_guest_contact_spam_flags as backfill
from src.db.base import Base
from src.guest_contacts.models import GuestContact

BOT = dict(
    first_name="ussppyXbAPxyhvUi",
    last_name="BDJZjHHdCzIsbpyEIDpPMZsn",
    email="buni.q.in783@gmail.com",
    school_name="CUDKcLwIRfACLYGlbxLABui",
    message="7452959171",
)
PARENT = dict(
    first_name="Patricia",
    last_name="Davico",
    email="patricia@example.com",
    school_name="St. Ignatius College Prep",
    message="My daughter is a senior and we need help with the CSS Profile.",
)


@pytest.fixture
def session_factory(monkeypatch):
    """Point the script at an in-memory guest_contacts table."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [t for n, t in Base.metadata.tables.items() if n == "guest_contacts"]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(backfill, "get_session_factory", lambda: factory)
    monkeypatch.setattr(backfill.sys, "argv", ["backfill", "--apply"])
    return factory


def _seed(factory, **fields):
    db = factory()
    try:
        row = GuestContact(**fields)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _reload(factory, row_id):
    db = factory()
    try:
        return db.query(GuestContact).filter(GuestContact.id == row_id).one()
    finally:
        db.close()


def test_historical_bot_row_is_quarantined(session_factory):
    row_id = _seed(session_factory, **BOT)
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_spam, row.spam_reason) == (True, "gibberish_name")


def test_historical_enquiry_is_left_in_the_inbox(session_factory):
    row_id = _seed(session_factory, **PARENT)
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_spam, row.spam_reason) == (False, None)


def test_a_row_the_admin_rescued_survives_a_re_run(session_factory):
    """The whole reason restores are recorded rather than blanked."""
    row_id = _seed(session_factory, **BOT, is_spam=True, spam_reason="restored_by_admin")
    db = session_factory()
    try:  # what PATCH .../spam?is_spam=false leaves behind
        db.query(GuestContact).filter(GuestContact.id == row_id).update(
            {"is_spam": False, "spam_reason": "restored_by_admin"}
        )
        db.commit()
    finally:
        db.close()

    backfill.main()
    backfill.main()

    row = _reload(session_factory, row_id)
    assert (row.is_spam, row.spam_reason) == (False, "restored_by_admin")


def test_a_row_the_admin_quarantined_by_hand_is_left_alone(session_factory):
    row_id = _seed(session_factory, **PARENT, is_spam=True, spam_reason="marked_by_admin")
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_spam, row.spam_reason) == (True, "marked_by_admin")
