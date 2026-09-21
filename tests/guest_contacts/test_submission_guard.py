"""The public contact endpoint's guards: rate limit, quarantine, admin override.

Quarantine, not rejection, is the contract worth pinning down here: a flagged
submission is still stored and the submitter is still told it went through, so a
misfiring heuristic never silently swallows a real enquiry.

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/auth/test_me_timezone_preference.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
from src.auth.deps import get_current_user
from src.auth.rate_limit import _hits
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.guest_contacts.models import GuestContact
from src.main import app

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

GOOD = {
    "first_name": "Patricia",
    "last_name": "Davico",
    "email": "patricia@example.com",
    "school_name": "St. Ignatius College Prep",
    "message": "My daughter is a senior and we need help with the CSS Profile.",
}
BOT = {
    "first_name": "ussppyXbAPxyhvUi",
    "last_name": "BDJZjHHdCzIsbpyEIDpPMZsn",
    "email": "buni.q.in783@gmail.com",
    "school_name": "CUDKcLwIRfACLYGlbxLABui",
    "message": "7452959171",
}


@pytest.fixture
def client():
    """A TestClient over an empty guest_contacts table, acting as super_admin."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [t for n, t in Base.metadata.tables.items() if n == "guest_contacts"]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_ID, role="super_admin", school_id=None
    )
    # The limiter is process-global, so each test starts from a clean window.
    _hits.clear()

    c = TestClient(app)
    c._session_local = SessionLocal  # stashed for assertions
    yield c

    app.dependency_overrides.clear()
    _hits.clear()


def _stored(client):
    db = client._session_local()
    try:
        return db.query(GuestContact).order_by(GuestContact.created_at).all()
    finally:
        db.close()


def _post(client, body, ip="203.0.113.7"):
    return client.post("/api/v1/guest-contacts", json=body, headers={"x-forwarded-for": ip})


# ── Quarantine ───────────────────────────────────────────────────────

def test_genuine_enquiry_is_not_flagged(client):
    assert _post(client, GOOD).status_code == 201
    (row,) = _stored(client)
    assert (row.is_spam, row.spam_reason) == (False, None)


def test_bot_submission_is_quarantined_but_still_answers_201(client):
    """The bot is told it succeeded — nothing signals which rule caught it."""
    resp = _post(client, BOT)
    assert resp.status_code == 201
    (row,) = _stored(client)
    assert row.is_spam is True
    assert row.spam_reason == "gibberish_name"


def test_honeypot_field_quarantines_an_otherwise_clean_submission(client):
    assert _post(client, {**GOOD, "website": "http://spam.example"}).status_code == 201
    (row,) = _stored(client)
    assert row.spam_reason == "honeypot"


def test_honeypot_value_is_never_persisted(client):
    """It is a trap, not data — it must not reach a column."""
    _post(client, {**GOOD, "website": "http://spam.example"})
    (row,) = _stored(client)
    assert not hasattr(row, "website")


# ── Rate limit ───────────────────────────────────────────────────────

def test_flood_from_one_address_is_cut_off(client):
    for _ in range(3):
        assert _post(client, GOOD).status_code == 201
    assert _post(client, GOOD).status_code == 429


def test_limit_is_per_address_so_one_flooder_cannot_block_others(client):
    for _ in range(3):
        _post(client, GOOD, ip="198.51.100.1")
    assert _post(client, GOOD, ip="198.51.100.1").status_code == 429
    assert _post(client, GOOD, ip="203.0.113.99").status_code == 201


def test_a_forged_forwarding_header_does_not_buy_a_fresh_window(client):
    """Only the entry the load balancer appended counts — the rest is caller input."""
    for _ in range(3):
        _post(client, GOOD, ip="198.51.100.1")
    assert _post(client, GOOD, ip="1.2.3.4, 198.51.100.1").status_code == 429


def test_a_family_sending_a_follow_up_is_not_blocked(client):
    """Two notes minutes apart is ordinary behaviour, seen in real traffic."""
    assert _post(client, GOOD).status_code == 201
    assert _post(client, {**GOOD, "message": "Adding that we are in California."}).status_code == 201


# ── Field validation ─────────────────────────────────────────────────

def test_malformed_email_is_rejected(client):
    assert _post(client, {**GOOD, "email": "not-an-address"}).status_code == 422


def test_oversized_message_is_rejected(client):
    assert _post(client, {**GOOD, "message": "x" * 5001}).status_code == 422


# ── Admin views and override ─────────────────────────────────────────

def test_listing_defaults_to_the_inbox_and_spam_is_opt_in(client):
    _post(client, GOOD)
    _post(client, BOT)

    inbox = client.get("/api/v1/guest-contacts").json()
    assert [r["first_name"] for r in inbox] == ["Patricia"]

    spam = client.get("/api/v1/guest-contacts", params={"spam": True}).json()
    assert [r["spam_reason"] for r in spam] == ["gibberish_name"]


def test_counts_cover_both_tabs(client):
    _post(client, GOOD)
    _post(client, BOT)
    assert client.get("/api/v1/guest-contacts/counts").json() == {"inbox": 1, "spam": 1}


def test_admin_can_rescue_a_false_positive(client):
    _post(client, BOT)
    (row,) = _stored(client)

    resp = client.patch(f"/api/v1/guest-contacts/{row.id}/spam", params={"is_spam": False})
    assert resp.status_code == 200
    assert resp.json()["is_spam"] is False
    # Not blanked: the rescue is recorded, which is what keeps a later re-run of
    # the backfill from quarantining the row all over again.
    assert resp.json()["spam_reason"] == "restored_by_admin"

    inbox = client.get("/api/v1/guest-contacts").json()
    assert len(inbox) == 1


def test_admin_can_quarantine_something_the_rules_missed(client):
    _post(client, GOOD)
    (row,) = _stored(client)

    client.patch(f"/api/v1/guest-contacts/{row.id}/spam", params={"is_spam": True})
    assert client.get("/api/v1/guest-contacts").json() == []
    spam = client.get("/api/v1/guest-contacts", params={"spam": True}).json()
    assert spam[0]["spam_reason"] == "marked_by_admin"


def test_public_response_does_not_reveal_the_spam_verdict(client):
    """A bot must not learn it was caught, or which rule caught it."""
    body = _post(client, BOT).json()
    assert set(body) == {"id", "created_at"}

    # The row really was quarantined — the silence is the point, not a no-op.
    (row,) = _stored(client)
    assert row.is_spam is True
