"""HTTP-level tests for the broadcast endpoints (super_admin only).

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/emails/test_unsubscribe.py / tests/auth/test_contact_auto_emails_self_edit.py.
The real send path (background task) runs synchronously in TestClient (FastAPI
executes background tasks before returning the response in tests), and
`email_send_enabled` defaults to False, so sends land as dry_run rows with no
boto3 calls — exercising the real dry-run pipeline end to end.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.broadcast_models import Broadcast
from src.emails.models import EmailSendLog
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex UUIDs — see tests/auth/test_contact_auto_emails_self_edit.py
# for the documented SQLite NUMERIC-affinity coercion bug this avoids.
ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_CONTACT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SCHOOL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
FAMILY_CONTACT_1_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
FAMILY_CONTACT_2_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
FAMILY_CONTACT_3_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

SIMPLE_DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi {{school_name}}"}]}]}


@pytest.fixture
def make_client(monkeypatch):
    """Factory: TestClient acting as `role`, with an admin contact (own login)
    and a customer school seeded with 3 opted-in family contacts."""

    def _build(role: str):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        tables = [
            t
            for n, t in Base.metadata.tables.items()
            if n in ("contacts", "schools", "cohorts", "grade_sets", "broadcast", "email_send_log", "email_suppression", "user_roles")
        ]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(School(id=SCHOOL_ID, name="Test High", is_current_customer=True))
        seed.add(
            Contact(
                id=ADMIN_CONTACT_ID,
                user_id=ADMIN_USER_ID,
                school_id=SCHOOL_ID,
                email="admin@example.com",
                first_name="Admin",
                last_name="Person",
                role="hub_admin",
                auto_emails=True,
            )
        )
        for cid, email in (
            (FAMILY_CONTACT_1_ID, "family1@example.com"),
            (FAMILY_CONTACT_2_ID, "family2@example.com"),
            (FAMILY_CONTACT_3_ID, "family3@example.com"),
        ):
            seed.add(
                Contact(
                    id=cid, school_id=SCHOOL_ID, email=email, role="hub_user", auto_emails=True
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

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_supabase] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=ADMIN_USER_ID, role=role, school_id=SCHOOL_ID
        )
        # The send background task opens its OWN session via `get_db()` directly
        # (never reuses the request-scoped session — see broadcast_send.py) —
        # that call bypasses FastAPI's dependency_overrides, so it must be
        # patched at the module level too, or it would hit the real configured
        # database instead of this test's in-memory one.
        monkeypatch.setattr("src.emails.broadcast_send.get_db", override_get_db)

        client = TestClient(app)
        client._session_local = SessionLocal
        return client

    yield _build
    app.dependency_overrides.clear()


def _create_broadcast(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={
            "subject": "Hello {{school_name}}",
            "body_json": SIMPLE_DOC,
            "school_scope": str(SCHOOL_ID),
            "role_filter": "all",
            "opt_in_filter": "opted_in",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Authorization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_create_broadcast(make_client, role: str):
    client = make_client(role)
    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={"subject": "x", "body_json": SIMPLE_DOC, "school_scope": "all_customers"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_list_broadcasts(make_client, role: str):
    client = make_client(role)
    resp = client.get("/api/v1/emails/broadcasts")
    assert resp.status_code == 403


def test_super_admin_can_create_broadcast(make_client):
    client = make_client("super_admin")
    body = _create_broadcast(client)
    assert body["status"] == "draft"
    assert body["subject"] == "Hello {{school_name}}"


# ── Audience preview ─────────────────────────────────────────────────────────


def test_audience_preview_reports_matched_count(make_client):
    client = make_client("super_admin")
    resp = client.get(
        "/api/v1/emails/broadcasts/audience-preview",
        params={"school_scope": str(SCHOOL_ID), "role_filter": "all", "opt_in_filter": "opted_in"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 4  # admin + 3 family contacts, all opted in
    assert body["warning"] is False


# ── Send (dry-run) ───────────────────────────────────────────────────────────


def test_send_broadcast_dry_run_creates_one_log_row_per_recipient_with_merge_substitution(make_client):
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")
    assert resp.status_code == 202
    assert resp.json()["recipient_count"] == "4"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert len(logs) == 4
        assert {log.status for log in logs} == {"dry_run"}
        # Merge tag resolved per recipient — {{school_name}} -> "Test High"
        assert all("Test High" in (log.rendered_html or "") for log in logs)

        updated = db.query(Broadcast).filter(Broadcast.id == uuid.UUID(broadcast["id"])).first()
        assert updated.status == "sent"
    finally:
        db.close()


def test_send_broadcast_twice_is_rejected(make_client):
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    first = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")
    assert first.status_code == 202

    second = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")
    assert second.status_code == 409


def test_send_test_broadcast_sends_only_to_requesting_admin(make_client):
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send-test")
    assert resp.status_code == 200
    assert resp.json()["sent_to"] == "admin@example.com"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert len(logs) == 1
        assert logs[0].recipient_email == "admin@example.com"
    finally:
        db.close()


def test_send_test_falls_back_to_admin_login_email_when_no_contact(make_client):
    """Super_admins have no Contact row — the test send must fall back to their
    authenticated login email using a sample audience contact for context
    (regression: this path previously raised 400)."""
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", email="super@example.com"
    )

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send-test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sent_to"] == "super@example.com"
    assert body["used_sample_contact"] is True

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert len(logs) == 1
        assert logs[0].recipient_email == "super@example.com"
    finally:
        db.close()


def test_send_test_returns_400_when_admin_has_no_contact_and_no_email(make_client):
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", email=None
    )

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send-test")
    assert resp.status_code == 400


def test_get_broadcast_detail_reports_status_counts(make_client):
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)
    client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")

    resp = client.get(f"/api/v1/emails/broadcasts/{broadcast['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run_count"] == 4
    assert body["sent_count"] == 0
    assert len(body["recipients"]) == 4


def test_get_unknown_broadcast_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.get(f"/api/v1/emails/broadcasts/{uuid.uuid4()}")
    assert resp.status_code == 404
