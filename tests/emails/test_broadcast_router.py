"""HTTP-level tests for the broadcast endpoints (super_admin only).

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/emails/test_unsubscribe.py / tests/auth/test_contact_auto_emails_self_edit.py.
The real send path (background task) runs synchronously in TestClient (FastAPI
executes background tasks before returning the response in tests). Each client
seeds an ``AppConfig`` row with ``email_sandbox_mode=True``, so recipients
outside the team domain (all seeded contacts use ``@example.com``) land as
``sandboxed`` rows with no boto3 calls — exercising the real send pipeline end
to end without touching SES.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.app_config.models import AppConfig
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
# A NOT-opted-in contact of the same customer school — excluded by the default
# opted_in filter, but an admin may deliberately keep them in the edited set.
FAMILY_NO_OPTIN_ID = uuid.UUID("abababab-abab-abab-abab-abababababab")
# A contact of a NON-customer school — must never be reachable, even if an admin
# forges its id into the recipient list.
NON_CUSTOMER_SCHOOL_ID = uuid.UUID("acacacac-acac-acac-acac-acacacacacac")
NON_CUSTOMER_CONTACT_ID = uuid.UUID("adadadad-adad-adad-adad-adadadadadad")

SIMPLE_DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi {{school_name}}"}]}]}
# Body merge tags are `mergeTag` nodes (chips), not raw "{{tag}}" text — only
# the subject line is a plain string (see link_resolver.resolve_merge_tag).
GREETING_DOC = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Hi "},
                {"type": "mergeTag", "attrs": {"tag": "recipient_first_names"}},
                {"type": "text", "text": ","},
            ],
        }
    ],
}


@pytest.fixture
def make_client(monkeypatch):
    """Factory: TestClient acting as `role`, with an admin contact (own login)
    and a customer school seeded with 3 opted-in family contacts."""

    def _build(role: str):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        tables = [
            t
            for n, t in Base.metadata.tables.items()
            if n in ("contacts", "schools", "cohorts", "grade_sets", "broadcast", "email_send_log", "email_suppression", "user_roles", "app_config")
        ]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        # Sandbox on: every seeded contact is @example.com (outside the team
        # domain), so sends are withheld and logged as "sandboxed" — no SES.
        seed.add(AppConfig(email_sandbox_mode=True))
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
                broadcast_emails=True,
            )
        )
        for cid, email in (
            (FAMILY_CONTACT_1_ID, "family1@example.com"),
            (FAMILY_CONTACT_2_ID, "family2@example.com"),
            (FAMILY_CONTACT_3_ID, "family3@example.com"),
        ):
            seed.add(
                Contact(
                    id=cid, school_id=SCHOOL_ID, email=email, role="hub_user", broadcast_emails=True
                )
            )
        seed.add(
            Contact(
                id=FAMILY_NO_OPTIN_ID,
                school_id=SCHOOL_ID,
                email="nooptin@example.com",
                role="hub_user",
                broadcast_emails=False,
            )
        )
        seed.add(School(id=NON_CUSTOMER_SCHOOL_ID, name="Prospect School", is_current_customer=False))
        seed.add(
            Contact(
                id=NON_CUSTOMER_CONTACT_ID,
                school_id=NON_CUSTOMER_SCHOOL_ID,
                email="prospect@example.com",
                role="hub_user",
                broadcast_emails=True,
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
            "school_ids": [str(SCHOOL_ID)],
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
        json={"subject": "x", "body_json": SIMPLE_DOC, "school_ids": []},
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
        params={"school_ids": [str(SCHOOL_ID)], "role_filter": "all", "opt_in_filter": "opted_in"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_count"] == 4  # admin + 3 family contacts, all opted in
    assert body["warning"] is False


# ── Audience preview: full contact list ──────────────────────────────────────


def test_audience_preview_contacts_returns_resolved_rows(make_client):
    client = make_client("super_admin")
    resp = client.get(
        "/api/v1/emails/broadcasts/audience-preview/contacts",
        params={"school_ids": [str(SCHOOL_ID)], "role_filter": "all", "opt_in_filter": "opted_in"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    emails = {r["email"] for r in rows}
    assert emails == {"admin@example.com", "family1@example.com", "family2@example.com", "family3@example.com"}
    assert all(r["opted_in"] for r in rows)
    assert all(r["school_name"] == "Test High" for r in rows)


def test_audience_preview_contacts_includes_non_opted_in_when_filter_all(make_client):
    client = make_client("super_admin")
    resp = client.get(
        "/api/v1/emails/broadcasts/audience-preview/contacts",
        params={"school_ids": [str(SCHOOL_ID)], "role_filter": "all", "opt_in_filter": "all"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    no_optin = next(r for r in rows if r["email"] == "nooptin@example.com")
    assert no_optin["opted_in"] is False


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_preview_audience_contacts(make_client, role: str):
    client = make_client(role)
    resp = client.get(
        "/api/v1/emails/broadcasts/audience-preview/contacts",
        params={"school_ids": [str(SCHOOL_ID)]},
    )
    assert resp.status_code == 403


# ── Send (sandboxed) ─────────────────────────────────────────────────────────


def test_send_broadcast_sandboxed_creates_one_log_row_per_recipient_with_merge_substitution(make_client):
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
        # All recipients are @example.com (outside the team domain) and sandbox
        # mode is on, so every send is withheld and logged as "sandboxed".
        assert {log.status for log in logs} == {"sandboxed"}
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


# ── Send with an explicit, admin-edited recipient set ─────────────────────────


def test_send_broadcast_with_explicit_recipient_ids_sends_only_to_that_set(make_client):
    """An admin-edited recipient list is sent to exactly those contacts, ignoring
    the broadcast's stored filters."""
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(
        f"/api/v1/emails/broadcasts/{broadcast['id']}/send",
        json={"recipient_contact_ids": [str(FAMILY_CONTACT_1_ID), str(FAMILY_CONTACT_2_ID)]},
    )
    assert resp.status_code == 202
    assert resp.json()["recipient_count"] == "2"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert {log.recipient_email for log in logs} == {"family1@example.com", "family2@example.com"}
    finally:
        db.close()


def test_send_broadcast_with_explicit_ids_keeps_manually_added_non_opted_in(make_client):
    """A non-opted-in contact deliberately kept in the edited set IS sent to —
    the opt-in filter does not override an explicit admin choice."""
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(
        f"/api/v1/emails/broadcasts/{broadcast['id']}/send",
        json={"recipient_contact_ids": [str(FAMILY_CONTACT_1_ID), str(FAMILY_NO_OPTIN_ID)]},
    )
    assert resp.status_code == 202
    assert resp.json()["recipient_count"] == "2"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert {log.recipient_email for log in logs} == {"family1@example.com", "nooptin@example.com"}
    finally:
        db.close()


def test_send_broadcast_with_explicit_ids_drops_non_customer_school_contact(make_client):
    """A forged/stale id pointing at a non-customer school is silently dropped —
    the server-side customer-school guard is non-negotiable."""
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(
        f"/api/v1/emails/broadcasts/{broadcast['id']}/send",
        json={"recipient_contact_ids": [str(FAMILY_CONTACT_1_ID), str(NON_CUSTOMER_CONTACT_ID)]},
    )
    assert resp.status_code == 202
    assert resp.json()["recipient_count"] == "1"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert {log.recipient_email for log in logs} == {"family1@example.com"}
    finally:
        db.close()


def test_send_broadcast_without_body_falls_back_to_stored_filters(make_client):
    """Omitting the body re-resolves the audience from the broadcast's stored
    filters — backward compatible with the pre-editing send flow."""
    client = make_client("super_admin")
    broadcast = _create_broadcast(client)

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send", json={})
    assert resp.status_code == 202
    assert resp.json()["recipient_count"] == "4"


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
    assert body["sandboxed_count"] == 4
    assert body["sent_count"] == 0
    assert len(body["recipients"]) == 4


def test_get_unknown_broadcast_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.get(f"/api/v1/emails/broadcasts/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Sender identity ──────────────────────────────────────────────────────────


def test_create_broadcast_rejects_sender_outside_allowed_domains(make_client):
    """SES would reject an unverified identity per recipient at send time — the
    400 here turns that into one actionable error at save time."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={
            "subject": "x",
            "body_json": SIMPLE_DOC,
            "school_ids": [],
            "sender_name": "Someone Else",
            "sender_email": "spoof@evil.example",
        },
    )
    assert resp.status_code == 400
    assert "collegemoneymethod.com" in resp.json()["detail"]


def test_create_broadcast_stores_allowed_custom_sender(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={
            "subject": "x",
            "body_json": SIMPLE_DOC,
            "school_ids": [],
            "sender_name": "CMM Newsflash",
            "sender_email": "newsflash@collegemoneymethod.com",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sender_name"] == "CMM Newsflash"
    assert body["sender_email"] == "newsflash@collegemoneymethod.com"


def test_sender_options_lists_presets_and_allowed_domains(make_client):
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/broadcasts/sender-options")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowed_domains"] == ["collegemoneymethod.com"]
    assert any(p["email"] == "newsflash@collegemoneymethod.com" for p in body["presets"])


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_read_sender_options(make_client, role: str):
    client = make_client(role)
    assert client.get("/api/v1/emails/broadcasts/sender-options").status_code == 403


# ── Grouped send (one email per school) ──────────────────────────────────────


def _create_grouped_broadcast(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={
            "subject": "Hello {{school_name}}",
            "body_json": GREETING_DOC,
            "school_ids": [str(SCHOOL_ID)],
            "group_by_school": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_grouped_send_logs_one_row_per_recipient_of_the_single_email(make_client):
    """One email goes out, but each addressee still gets its own log row —
    status counts, the recipient table and open rates all read a row as one
    person, so collapsing them would undercount the send's reach."""
    client = make_client("super_admin")
    broadcast = _create_grouped_broadcast(client)

    resp = client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")
    assert resp.status_code == 202
    # The audience is still 4 contacts — they just collapse into one email.
    assert resp.json()["recipient_count"] == "4"

    db = client._session_local()
    try:
        logs = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .all()
        )
        assert {log.recipient_email for log in logs} == {
            "admin@example.com",
            "family1@example.com",
            "family2@example.com",
            "family3@example.com",
        }
        assert len(logs) == 4
    finally:
        db.close()


def test_grouped_send_detail_counts_every_recipient(make_client):
    """The admin-facing summary must report 4 recipients, not 1 email."""
    client = make_client("super_admin")
    broadcast = _create_grouped_broadcast(client)
    client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")

    detail = client.get(f"/api/v1/emails/broadcasts/{broadcast['id']}").json()
    assert detail["sandboxed_count"] == 4
    assert len(detail["recipients"]) == 4
    # Each row names exactly one person — never a comma-joined list.
    assert all("," not in r["recipient_email"] for r in detail["recipients"])


def test_grouped_send_renders_every_recipient_first_name(make_client):
    client = make_client("super_admin")
    # Give each recipient a first name so the joined greeting is observable.
    db = client._session_local()
    try:
        for cid, first in (
            (FAMILY_CONTACT_1_ID, "Paul"),
            (FAMILY_CONTACT_2_ID, "Caroline"),
            (FAMILY_CONTACT_3_ID, "Vu"),
        ):
            db.query(Contact).filter(Contact.id == cid).first().first_name = first
        db.query(Contact).filter(Contact.id == ADMIN_CONTACT_ID).first().broadcast_emails = False
        db.commit()
    finally:
        db.close()

    broadcast = _create_grouped_broadcast(client)
    client.post(f"/api/v1/emails/broadcasts/{broadcast['id']}/send")

    db = client._session_local()
    try:
        log = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .first()
        )
        assert "Hi Paul, Caroline and Vu," in (log.rendered_html or "")
    finally:
        db.close()


def test_ungrouped_send_renders_the_single_recipients_first_name(make_client):
    """The same merge tag reads naturally on a normal, per-contact send."""
    client = make_client("super_admin")
    db = client._session_local()
    try:
        db.query(Contact).filter(Contact.id == FAMILY_CONTACT_1_ID).first().first_name = "Paul"
        db.commit()
    finally:
        db.close()

    resp = client.post(
        "/api/v1/emails/broadcasts",
        json={
            "subject": "Hi",
            "body_json": GREETING_DOC,
            "school_ids": [str(SCHOOL_ID)],
        },
    )
    broadcast = resp.json()
    client.post(
        f"/api/v1/emails/broadcasts/{broadcast['id']}/send",
        json={"recipient_contact_ids": [str(FAMILY_CONTACT_1_ID)]},
    )

    db = client._session_local()
    try:
        log = (
            db.query(EmailSendLog)
            .filter(EmailSendLog.broadcast_id == uuid.UUID(broadcast["id"]))
            .first()
        )
        assert "Hi Paul," in (log.rendered_html or "")
    finally:
        db.close()
