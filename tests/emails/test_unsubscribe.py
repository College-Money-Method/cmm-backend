"""Tests for the public, no-login CAN-SPAM unsubscribe flow.

Covers both layers: the signed-token helpers (unsubscribe.py) directly, and the
end-to-end HTTP endpoint (unsubscribe_router.py) via a TestClient backed by an
in-memory SQLite DB, following the pattern in
tests/schools/test_public_school_access.py.
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
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.models import EmailSuppression
from src.emails.unsubscribe import generate_unsubscribe_token, verify_unsubscribe_token
from src.main import app
from src.schools.models import Contact

CONTACT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CONTACT_EMAIL = "counselor@example.com"


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
    seed.add(Contact(id=CONTACT_ID, email=CONTACT_EMAIL, auto_emails=True))
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


def test_valid_token_flips_auto_emails_and_records_suppression(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = client.get(f"/api/v1/emails/unsubscribe?token={token}")
    assert resp.status_code == 200
    # No PII/contact details in the response body.
    assert str(CONTACT_ID) not in resp.text
    assert CONTACT_EMAIL not in resp.text

    db = _session_factory(client)
    try:
        contact = db.query(Contact).filter(Contact.id == CONTACT_ID).first()
        assert contact.auto_emails is False
        suppression = db.query(EmailSuppression).filter(EmailSuppression.email == CONTACT_EMAIL).first()
        assert suppression is not None
        assert suppression.reason == "unsubscribe"
    finally:
        db.close()


def test_forged_token_rejected_without_auth(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    resp = client.get(f"/api/v1/emails/unsubscribe?token={_forge(token)}")
    assert resp.status_code == 400

    db = _session_factory(client)
    try:
        contact = db.query(Contact).filter(Contact.id == CONTACT_ID).first()
        # Untouched — the forged token must never reach the DB write path.
        assert contact.auto_emails is True
    finally:
        db.close()


def test_expired_token_rejected(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID, ttl_seconds=-1)
    resp = client.get(f"/api/v1/emails/unsubscribe?token={token}")
    assert resp.status_code == 400


def test_valid_token_for_unknown_contact_returns_generic_success(client: TestClient):
    """A structurally-valid, correctly-signed token for a contact that no longer
    exists must not error out or leak that the contact is missing — the visitor
    just sees the same generic confirmation."""
    token = generate_unsubscribe_token(uuid.uuid4())
    resp = client.get(f"/api/v1/emails/unsubscribe?token={token}")
    assert resp.status_code == 200


def test_repeated_unsubscribe_click_is_idempotent(client: TestClient):
    token = generate_unsubscribe_token(CONTACT_ID)
    first = client.get(f"/api/v1/emails/unsubscribe?token={token}")
    second = client.get(f"/api/v1/emails/unsubscribe?token={token}")
    assert first.status_code == 200
    assert second.status_code == 200

    db = _session_factory(client)
    try:
        count = (
            db.query(EmailSuppression).filter(EmailSuppression.email == CONTACT_EMAIL).count()
        )
        assert count == 1
    finally:
        db.close()
