"""Tests for the public, no-login CAN-SPAM email-preference flow.

Covers both layers: the signed-token helpers (unsubscribe.py) directly, and the
end-to-end HTTP API (email_preferences_router.py) that the frontend preference
page calls, via a TestClient backed by an in-memory SQLite DB, following the
pattern in tests/schools/test_public_school_access.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.emails.models  # noqa: F401 — register EmailSuppression for FK metadata
import src.schools.models  # noqa: F401
from src.config import settings
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.models import EmailSuppression
from src.emails.unsubscribe import (
    build_unsubscribe_url,
    generate_unsubscribe_token,
    verify_unsubscribe_token,
    verify_unsubscribe_token_ids,
)
from src.main import app
from src.schools.models import Contact

CONTACT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CONTACT_EMAIL = "counselor@example.com"
# Second recipient of a grouped (one email, several counselors) send.
CONTACT_2_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
CONTACT_2_EMAIL = "director@example.com"


# ── Token helpers (no DB, no HTTP) ──────────────────────────────────────────


def test_valid_token_round_trips_contact_id():
    token = generate_unsubscribe_token(CONTACT_ID)
    assert verify_unsubscribe_token(token) == CONTACT_ID


def _forge(token: str) -> str:
    """Flip one character to corrupt `token`'s embedded signature.

    Deliberately avoids the last few characters: base64's trailing group (when
    the payload isn't a multiple of 3 bytes, which is the common case here)
    encodes some "don't care" padding bits that a flip can land on without
    changing the decoded bytes at all, making the corruption a no-op. Flipping
    well before that tail group always changes the decoded payload.
    """
    pos = len(token) - 6
    return token[:pos] + ("a" if token[pos] != "a" else "b") + token[pos + 1:]


def test_forged_signature_is_rejected():
    token = generate_unsubscribe_token(CONTACT_ID)
    assert verify_unsubscribe_token(_forge(token)) is None


def test_expired_token_is_rejected():
    token = generate_unsubscribe_token(CONTACT_ID, ttl_seconds=-1)
    assert verify_unsubscribe_token(token) is None


def test_malformed_token_is_rejected():
    assert verify_unsubscribe_token("not-a-valid-token") is None
    assert verify_unsubscribe_token("") is None


def test_token_for_different_contact_does_not_verify_as_this_one():
    token = generate_unsubscribe_token(uuid.uuid4())
    assert verify_unsubscribe_token(token) != CONTACT_ID


# ── HTTP endpoint (in-memory DB) ─────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """TestClient backed by an in-memory SQLite DB seeded with one contact."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        t for n, t in Base.metadata.tables.items() if n in ("contacts", "schools", "email_suppression")
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(Contact(id=CONTACT_ID, email=CONTACT_EMAIL, auto_emails=True, broadcast_emails=True))
    seed.add(
        Contact(id=CONTACT_2_ID, email=CONTACT_2_EMAIL, auto_emails=True, broadcast_emails=True)
    )
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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _session_factory(client: TestClient):
    # Rebuild a session against the same overridden dependency for assertions.
    gen = app.dependency_overrides[get_db]()
    return next(gen)


PREFERENCES_URL = "/api/v1/emails/preferences"


def _save(client: TestClient, token: str, *, auto: bool, broadcast: bool):
    return client.put(
        PREFERENCES_URL,
        json={"token": token, "auto_emails": auto, "broadcast_emails": broadcast},
    )


def _contact(client: TestClient, contact_id: uuid.UUID) -> Contact:
    db = _session_factory(client)
    try:
        return db.query(Contact).filter(Contact.id == contact_id).first()
    finally:
        db.close()


def _suppression(client: TestClient, email: str) -> EmailSuppression | None:
    db = _session_factory(client)
    try:
        return db.query(EmailSuppression).filter(EmailSuppression.email == email).first()
    finally:
        db.close()


def test_footer_link_points_at_the_frontend_page_not_the_api(monkeypatch):
    """Recipients should only ever see the site's own origin."""
    monkeypatch.setattr(settings, "app_public_url", "https://next.example.com")
    url = build_unsubscribe_url(CONTACT_ID)
    assert url.startswith("https://next.example.com/email-preferences?token=")
    assert "/api/" not in url


def test_already_sent_links_redirect_to_the_frontend_page(client: TestClient, monkeypatch):
    """Tokens live a year, so links posted before the page moved must keep working."""
    monkeypatch.setattr(settings, "app_public_url", "https://next.example.com")
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = client.get(f"/api/v1/emails/unsubscribe?token={token}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://next.example.com/email-preferences?token={token}"


def test_reading_preferences_changes_nothing(client: TestClient):
    """A mail client or link scanner prefetching the footer link must not be
    able to unsubscribe somebody — only the PUT changes state."""
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = client.get(f"{PREFERENCES_URL}?token={token}")
    assert resp.status_code == 200
    assert resp.json() == {"auto_emails": True, "broadcast_emails": True}
    # No PII/contact details in the response body.
    assert str(CONTACT_ID) not in resp.text
    assert CONTACT_EMAIL not in resp.text

    contact = _contact(client, CONTACT_ID)
    assert contact.auto_emails is True
    assert contact.broadcast_emails is True
    assert _suppression(client, CONTACT_EMAIL) is None


def test_saving_preferences_moves_the_two_opt_ins_independently(client: TestClient):
    """Declining broadcasts while keeping workshop automations must not suppress
    the address — they would then receive neither."""
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = _save(client, token, auto=True, broadcast=False)
    assert resp.status_code == 200
    assert resp.json() == {"auto_emails": True, "broadcast_emails": False}

    contact = _contact(client, CONTACT_ID)
    assert contact.auto_emails is True
    assert contact.broadcast_emails is False
    assert _suppression(client, CONTACT_EMAIL) is None


def test_turning_both_opt_ins_off_records_suppression(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    assert _save(client, token, auto=False, broadcast=False).status_code == 200

    contact = _contact(client, CONTACT_ID)
    assert contact.auto_emails is False
    assert contact.broadcast_emails is False
    suppression = _suppression(client, CONTACT_EMAIL)
    assert suppression is not None and suppression.reason == "unsubscribe"


def test_opting_back_in_lifts_the_earlier_unsubscribe_suppression(client: TestClient):
    """Suppression blocks every send whatever the opt-ins say, so re-subscribing
    has to clear it or the contact keeps receiving nothing."""
    token = generate_unsubscribe_token(CONTACT_ID)
    _save(client, token, auto=False, broadcast=False)
    assert _suppression(client, CONTACT_EMAIL) is not None

    _save(client, token, auto=True, broadcast=True)

    contact = _contact(client, CONTACT_ID)
    assert contact.auto_emails is True
    assert contact.broadcast_emails is True
    assert _suppression(client, CONTACT_EMAIL) is None


def test_opting_back_in_leaves_a_bounce_suppression_in_place(client: TestClient):
    """A bounce is the receiving server's decision, not the recipient's — no
    preference change may clear it."""
    db = _session_factory(client)
    try:
        db.add(EmailSuppression(email=CONTACT_EMAIL, reason="bounce"))
        db.commit()
    finally:
        db.close()

    token = generate_unsubscribe_token(CONTACT_ID)
    _save(client, token, auto=True, broadcast=True)

    suppression = _suppression(client, CONTACT_EMAIL)
    assert suppression is not None and suppression.reason == "bounce"


def test_forged_token_rejected_on_read(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = client.get(f"{PREFERENCES_URL}?token={_forge(token)}")
    assert resp.status_code == 400

    # Untouched — the forged token must never reach the DB write path.
    assert _contact(client, CONTACT_ID).auto_emails is True


def test_forged_token_rejected_on_save(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = _save(client, _forge(token), auto=False, broadcast=False)
    assert resp.status_code == 400

    assert _contact(client, CONTACT_ID).auto_emails is True
    assert _suppression(client, CONTACT_EMAIL) is None


def test_expired_token_rejected(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID, ttl_seconds=-1)
    assert client.get(f"{PREFERENCES_URL}?token={token}").status_code == 400


def test_valid_token_for_unknown_contact_returns_generic_state(client: TestClient):
    """A structurally-valid, correctly-signed token for a contact that no longer
    exists must not error out or leak that the contact is missing."""
    token = generate_unsubscribe_token(uuid.uuid4())
    resp = client.get(f"{PREFERENCES_URL}?token={token}")
    assert resp.status_code == 200
    assert resp.json() == {"auto_emails": False, "broadcast_emails": False}


def test_repeated_unsubscribe_is_idempotent(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    assert _save(client, token, auto=False, broadcast=False).status_code == 200
    assert _save(client, token, auto=False, broadcast=False).status_code == 200

    db = _session_factory(client)
    try:
        count = db.query(EmailSuppression).filter(EmailSuppression.email == CONTACT_EMAIL).count()
        assert count == 1
    finally:
        db.close()


def test_multi_contact_token_round_trips_every_id():
    token = generate_unsubscribe_token([CONTACT_ID, CONTACT_2_ID])
    assert verify_unsubscribe_token_ids(token) == [CONTACT_ID, CONTACT_2_ID]


def test_single_id_token_still_verifies_as_a_one_element_list():
    """Tokens minted before grouped sends existed must keep working."""
    token = generate_unsubscribe_token(CONTACT_ID)
    assert verify_unsubscribe_token_ids(token) == [CONTACT_ID]


def test_grouped_token_applies_the_choice_to_every_recipient(client: TestClient):
    """Each recipient of a grouped send sees the same footer link, so whoever
    clicks must be able to stop the mail they received."""
    token = generate_unsubscribe_token([CONTACT_ID, CONTACT_2_ID])
    assert _save(client, token, auto=False, broadcast=False).status_code == 200

    for cid in (CONTACT_ID, CONTACT_2_ID):
        contact = _contact(client, cid)
        assert contact.auto_emails is False
        assert contact.broadcast_emails is False
    for email in (CONTACT_EMAIL, CONTACT_2_EMAIL):
        assert _suppression(client, email) is not None


def test_grouped_token_reports_anyone_still_opted_in(client: TestClient):
    """A colleague's earlier opt-out must not silently propose unsubscribing
    everyone else."""
    db = _session_factory(client)
    try:
        contact = db.query(Contact).filter(Contact.id == CONTACT_2_ID).first()
        contact.auto_emails = False
        contact.broadcast_emails = False
        db.commit()
    finally:
        db.close()

    token = generate_unsubscribe_token([CONTACT_ID, CONTACT_2_ID])
    resp = client.get(f"{PREFERENCES_URL}?token={token}")
    assert resp.json() == {"auto_emails": True, "broadcast_emails": True}


def test_forged_multi_contact_token_touches_nobody(client: TestClient):
    token = generate_unsubscribe_token([CONTACT_ID, CONTACT_2_ID])
    resp = _save(client, _forge(token), auto=False, broadcast=False)
    assert resp.status_code == 400

    db = _session_factory(client)
    try:
        assert db.query(EmailSuppression).count() == 0
    finally:
        db.close()
