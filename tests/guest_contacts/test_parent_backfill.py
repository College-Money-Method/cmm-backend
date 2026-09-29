"""Filing the rows that predate the Parents tab, and the re-run it must survive.

Same shape as test_spam_backfill.py, and the same property carries the weight: a
second pass leaves every admin decision standing, in particular a row an admin
pushed back into the inbox.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.backfill import backfill_guest_contact_parent_flags as backfill
from src.db.base import Base
from src.guest_contacts.models import GuestContact

PARENT = dict(
    first_name="Dana",
    last_name="Whitfield",
    email="dana@example.com",
    school_name="Campbell Hall",
    message="My daughter is a senior and we need help with the CSS Profile.",
)
COUNSELLOR = dict(
    first_name="Patricia",
    last_name="Davico",
    email="patricia@example.com",
    school_name="St. Ignatius College Prep",
    message="I would like to learn more about how you might work with our families.",
)


@pytest.fixture
def session_factory(monkeypatch):
    """Run the script against an in-memory guest_contacts table.

    The script loads its dotenv and imports the session factory inside main(), so
    both are stubbed at their source rather than on the script's namespace.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [t for n, t in Base.metadata.tables.items() if n == "guest_contacts"]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("src.db.base.get_session_factory", lambda: factory)
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


def test_a_historical_family_enquiry_is_filed_under_parents(session_factory):
    row_id = _seed(session_factory, **PARENT)
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_parent, row.parent_reason) == (True, "mentions_own_child")


def test_a_historical_school_enquiry_stays_in_the_inbox(session_factory):
    row_id = _seed(session_factory, **COUNSELLOR)
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_parent, row.parent_reason) == (False, None)


def test_filing_a_row_never_changes_its_spam_verdict(session_factory):
    row_id = _seed(session_factory, **PARENT)
    backfill.main()
    assert _reload(session_factory, row_id).is_spam is False


def test_a_quarantined_row_is_left_out_of_the_parents_tab(session_factory):
    """Spam outranks audience — junk belongs in one tab, not two."""
    row_id = _seed(
        session_factory,
        **{**PARENT, "message": "my daughter 7452959171"},
        is_spam=True,
        spam_reason="gibberish_name",
    )
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_parent, row.parent_reason) == (False, None)


def test_a_row_the_admin_returned_to_the_inbox_survives_a_re_run(session_factory):
    """The whole reason restores are recorded rather than blanked."""
    row_id = _seed(session_factory, **PARENT, parent_reason="restored_by_admin")

    backfill.main()
    backfill.main()

    row = _reload(session_factory, row_id)
    assert (row.is_parent, row.parent_reason) == (False, "restored_by_admin")


def test_a_row_the_admin_filed_by_hand_is_left_alone(session_factory):
    row_id = _seed(session_factory, **COUNSELLOR, is_parent=True, parent_reason="marked_by_admin")
    backfill.main()
    row = _reload(session_factory, row_id)
    assert (row.is_parent, row.parent_reason) == (True, "marked_by_admin")
