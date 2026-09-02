"""HTTP-level tests for the template preview + send-test endpoints (super_admin
only). Uses the shared in-memory SQLite fixture (`scheduler_sessionmaker`) so
the workshop/webinar/portal_mapping tables — needed by the workshop-template
preview path — compile under SQLite. Sandbox mode is forced off and the SES
client is mocked, so send-test exercises the real send pipeline and lands as a
"sent" EmailSendLog row with no live boto3 call.
"""

from __future__ import annotations

import email
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.auth.models import UserRole
from src.emails.models import EmailSendLog
from src.main import app
from src.cycles.models import Cycle
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop, WorkshopRegistration

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_CONTACT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SCHOOL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
WORKSHOP_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
WEBINAR_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
CURRENT_CYCLE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OLD_CYCLE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
# Mapped to the same school but outside the current cycle — never offered by the
# preview's workshop picker.
OLD_WEBINAR_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CYCLELESS_WEBINAR_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
# A counselor with a hub login but a hub_user (not hub_admin) app role — still
# a counselor of the hub, so their own name must fill the counselor tags.
COUNSELOR_USER_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
COUNSELOR_CONTACT_ID = uuid.UUID("abababab-abab-abab-abab-abababababab")
# A login-less contact (no user_id) — NOT a counselor; falls back to the school's.
FAMILY_CONTACT_ID = uuid.UUID("acacacac-acac-acac-acac-acacacacacac")

BROADCAST_DOC = {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi {{school_name}}"}]}],
}
WORKSHOP_DOC = {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "See you at {{workshop_name}}"}]}],
}


@pytest.fixture
def make_client(scheduler_sessionmaker, monkeypatch):
    """Factory: TestClient acting as `role`, with a school, an admin (hub_admin)
    contact, and one workshop/webinar mapped to the school.

    Forces sandbox off and mocks the SES client so send-test lands as a "sent"
    row via the real pipeline — never a live SES call — regardless of the local
    .env."""

    monkeypatch.setattr("src.emails.ses_client._sandbox_enabled", lambda db: False)
    monkeypatch.setattr("src.config.settings.ses_from_email", "noreply@collegemoneymethod.com")
    mock_ses = MagicMock()
    mock_ses.send_raw_email.return_value = {"MessageId": "test-message-id"}
    monkeypatch.setattr("src.emails.ses_client._create_ses_client", lambda: mock_ses)

    def _build(role: str) -> TestClient:
        SessionLocal: sessionmaker = scheduler_sessionmaker

        seed = SessionLocal()
        seed.add(School(id=SCHOOL_ID, name="Test High", slug="test-high", is_current_customer=True))
        seed.add(
            Contact(
                id=ADMIN_CONTACT_ID,
                user_id=ADMIN_USER_ID,
                school_id=SCHOOL_ID,
                email="admin@collegemoneymethod.com",
                first_name="Admin",
                last_name="Person",
                # Airtable job title — display only; hub_admin comes from user_roles
                role="Director",
                auto_emails=True,
            )
        )
        # A counselor is anyone who can log into the hub (Contact.user_id set),
        # regardless of user_roles.role. Seed the admin's login (hub_admin), a
        # second counselor with a hub_user login, and a login-less family contact.
        seed.add(UserRole(user_id=ADMIN_USER_ID, school_id=SCHOOL_ID, role="hub_admin"))
        seed.add(
            Contact(
                id=COUNSELOR_CONTACT_ID,
                user_id=COUNSELOR_USER_ID,
                school_id=SCHOOL_ID,
                email="counselor@collegemoneymethod.com",
                first_name="Casey",
                last_name="Counselor",
                role="Counselor",
                auto_emails=True,
            )
        )
        seed.add(UserRole(user_id=COUNSELOR_USER_ID, school_id=SCHOOL_ID, role="hub_user"))
        seed.add(
            Contact(
                id=FAMILY_CONTACT_ID,
                school_id=SCHOOL_ID,
                email="family@example.com",
                first_name="Fran",
                last_name="Family",
                role="",
                auto_emails=True,
            )
        )
        seed.add(Cycle(id=CURRENT_CYCLE_ID, name="2025-26", is_current=True))
        seed.add(Cycle(id=OLD_CYCLE_ID, name="2024-25", is_current=False))
        seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
        seed.add(
            Webinar(
                id=WEBINAR_ID,
                workshop_id=WORKSHOP_ID,
                cycle_id=CURRENT_CYCLE_ID,
                start_datetime=datetime(2026, 4, 3, 18, 0, tzinfo=timezone.utc),
                registration_url="https://zoom.example.com/register",
            )
        )
        seed.add(PortalMapping(id=uuid.uuid4(), school_id=SCHOOL_ID, webinar_id=WEBINAR_ID))
        # Same school, same workshop, but a prior cycle — the picker must not
        # offer it, so the exclusion is exercised by every list assertion below.
        seed.add(
            Webinar(
                id=OLD_WEBINAR_ID,
                workshop_id=WORKSHOP_ID,
                cycle_id=OLD_CYCLE_ID,
                start_datetime=datetime(2025, 4, 3, 18, 0, tzinfo=timezone.utc),
                registration_url="https://zoom.example.com/register-old",
            )
        )
        seed.add(PortalMapping(id=uuid.uuid4(), school_id=SCHOOL_ID, webinar_id=OLD_WEBINAR_ID))
        # No cycle assigned at all — a stray/test webinar, also excluded.
        seed.add(
            Webinar(
                id=CYCLELESS_WEBINAR_ID,
                workshop_id=WORKSHOP_ID,
                start_datetime=datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc),
            )
        )
        seed.add(
            PortalMapping(id=uuid.uuid4(), school_id=SCHOOL_ID, webinar_id=CYCLELESS_WEBINAR_ID)
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

        client = TestClient(app)
        client._session_local = SessionLocal
        # Exposed so a test can assert the From identity the send went out with.
        client._mock_ses = mock_ses
        return client

    yield _build
    app.dependency_overrides.clear()


def _sent_html(client) -> str:
    """The text/html part of the last message handed to SES, decoded."""
    raw = client._mock_ses.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
    message = email.message_from_bytes(raw)
    for part in message.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf-8")
    raise AssertionError("no text/html part in the sent message")


def _broadcast_payload(**overrides) -> dict:
    payload = {
        "category": "general",
        "subject": "Hello {{school_name}}",
        "body_json": BROADCAST_DOC,
        "school_id": str(SCHOOL_ID),
    }
    payload.update(overrides)
    return payload


def _workshop_payload(**overrides) -> dict:
    payload = {
        "category": "workshop",
        "subject": "Reminder: {{workshop_name}}",
        "body_json": WORKSHOP_DOC,
        "school_id": str(SCHOOL_ID),
        "webinar_id": str(WEBINAR_ID),
    }
    payload.update(overrides)
    return payload


# ── Authorization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_render_preview(make_client, role: str):
    client = make_client(role)
    resp = client.post("/api/v1/emails/preview/render", json=_broadcast_payload())
    assert resp.status_code == 403


# ── Render ───────────────────────────────────────────────────────────────────


def test_render_broadcast_preview_resolves_school_tags(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/render", json=_broadcast_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "Hello Test High"
    assert "Test High" in body["html"]


def test_render_broadcast_preview_resolves_counselor_first_name(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(subject="Hi from {{counselor_first_name}}"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Admin"


def test_render_broadcast_preview_resolves_counselor_first_name_for_sample_contact(make_client):
    """Selecting a sample contact who IS a counselor (here the hub_admin) fills
    the counselor tags from that contact — the reported preview scenario."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Hi from {{counselor_first_name}}", contact_id=str(ADMIN_CONTACT_ID)
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Admin"


def test_render_broadcast_preview_uses_hub_user_counselors_own_name(make_client):
    """A sample contact who can log into the hub is a counselor even with a
    hub_user (non-admin) role — their OWN name must fill the counselor tags,
    not the school's director. (Regression: previously only hub_admin matched.)"""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Hi from {{counselor_first_name}}", contact_id=str(COUNSELOR_CONTACT_ID)
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Casey"


def test_render_broadcast_preview_login_less_contact_falls_back_to_school_counselor(make_client):
    """A login-less (no user_id) contact is NOT a counselor, so the counselor
    tags fall back to the school's representative counselor (the director)."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Hi from {{counselor_first_name}}", contact_id=str(FAMILY_CONTACT_ID)
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Admin"


def test_render_workshop_preview_resolves_counselor_first_name(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_workshop_payload(subject="Hi from {{counselor_first_name}}"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Admin"


def test_render_workshop_preview_resolves_workshop_tags(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/render", json=_workshop_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "Reminder: FAFSA Basics"
    assert "FAFSA Basics" in body["html"]


def test_render_workshop_preview_counts_this_schools_registrations(make_client):
    """`registrations_to_date`/`attendees` quote THIS school's numbers for the
    webinar — another school's registrations on the same webinar must not leak
    into them."""
    client = make_client("super_admin")
    other_school_id = uuid.uuid4()
    session = client._session_local()
    session.add(School(id=other_school_id, name="Other High", slug="other-high", is_current_customer=True))
    for email, attended in [("a@example.com", True), ("b@example.com", True), ("c@example.com", False)]:
        session.add(
            WorkshopRegistration(
                id=uuid.uuid4(), webinar_id=WEBINAR_ID, school_id=SCHOOL_ID, email=email, attended=attended
            )
        )
    session.add(
        WorkshopRegistration(
            id=uuid.uuid4(),
            webinar_id=WEBINAR_ID,
            school_id=other_school_id,
            email="elsewhere@example.com",
            attended=True,
        )
    )
    session.commit()
    session.close()
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_workshop_payload(subject="{{registrations_to_date}} signed up, {{attendees}} came"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "3 signed up, 2 came"


def test_render_workshop_preview_counts_are_zero_without_registrations(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_workshop_payload(subject="{{registrations_to_date}}/{{attendees}}"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "0/0"


def test_render_workshop_without_webinar_returns_422(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/render", json=_workshop_payload(webinar_id=None))
    assert resp.status_code == 422


def test_render_unknown_school_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render", json=_broadcast_payload(school_id=str(uuid.uuid4()))
    )
    assert resp.status_code == 404


# ── Grouped preview ──────────────────────────────────────────────────────────


def _set_opt_in(client, contact_ids: list[uuid.UUID]) -> None:
    """Opt the given contacts into broadcasts (the column defaults to False)."""
    session = client._session_local()
    for contact_id in contact_ids:
        session.get(Contact, contact_id).broadcast_emails = True
    session.commit()
    session.close()


def test_grouped_preview_greets_every_recipient(make_client):
    """The whole school audience shares one grouped email, so
    recipient_first_names must join all of their names — the tag is unusable in
    a preview otherwise (it would always show a single sample contact)."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Hi {{recipient_first_names}},", grouped=True, opt_in_filter="all"
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi Admin, Casey and Fran,"


def test_grouped_preview_keeps_duplicate_first_names(make_client):
    """Two counselors who share a first name must both be greeted ("Paul and
    Paul") — the grouped greeting is a list of recipients, not a set of names."""
    client = make_client("super_admin")
    paul_one, paul_two = uuid.uuid4(), uuid.uuid4()
    session = client._session_local()
    session.add(
        Contact(id=paul_one, school_id=SCHOOL_ID, email="paul.a@example.com", first_name="Paul", last_name="Marlin")
    )
    session.add(
        Contact(id=paul_two, school_id=SCHOOL_ID, email="paul.b@example.com", first_name="Paul", last_name="Munoz")
    )
    session.commit()
    session.close()
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Dear {{recipient_first_names}},",
            grouped=True,
            recipient_contact_ids=[str(paul_one), str(paul_two)],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Dear Paul and Paul,"


def test_grouped_preview_returns_the_to_line(make_client):
    """The preview reports who shares the email so the admin can sanity-check
    the To header before sending."""
    client = make_client("super_admin")
    _set_opt_in(client, [ADMIN_CONTACT_ID, COUNSELOR_CONTACT_ID])
    resp = client.post(
        "/api/v1/emails/preview/render", json=_broadcast_payload(grouped=True)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recipients"] == [
        "Admin Person <admin@collegemoneymethod.com>",
        "Casey Counselor <counselor@collegemoneymethod.com>",
    ]


def test_grouped_preview_applies_the_audience_filters(make_client):
    """Opted-out contacts are not on a real send's To header, so they must not
    appear in the grouped greeting either."""
    client = make_client("super_admin")
    _set_opt_in(client, [COUNSELOR_CONTACT_ID])
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(subject="Hi {{recipient_first_names}},", grouped=True),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi Casey,"


def test_grouped_preview_ignores_the_sample_contact(make_client):
    """A grouped email has no single "you" — the counselor tags come from the
    school's representative counselor even when a sample contact is picked."""
    client = make_client("super_admin")
    _set_opt_in(client, [COUNSELOR_CONTACT_ID, FAMILY_CONTACT_ID])
    resp = client.post(
        "/api/v1/emails/preview/render",
        json=_broadcast_payload(
            subject="Hi from {{counselor_first_name}}",
            grouped=True,
            contact_id=str(COUNSELOR_CONTACT_ID),
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Hi from Admin"


def test_ungrouped_preview_reports_no_recipient_list(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/render", json=_broadcast_payload())
    assert resp.status_code == 200, resp.text
    assert resp.json()["recipients"] == []


# ── Webinar picker ───────────────────────────────────────────────────────────


def test_list_school_webinars_returns_mapped_webinar(make_client):
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/preview/webinars", params={"school_id": str(SCHOOL_ID)})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["webinar_id"] == str(WEBINAR_ID)
    assert rows[0]["workshop_name"] == "FAFSA Basics"
    assert rows[0]["cycle_name"] == "2025-26"


def test_list_school_webinars_excludes_other_cycles_and_cycleless(make_client):
    """The school also has a prior-cycle webinar and a cycle-less one mapped to
    it; neither may reach the picker."""
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/preview/webinars", params={"school_id": str(SCHOOL_ID)})
    assert resp.status_code == 200, resp.text
    ids = {row["webinar_id"] for row in resp.json()}
    assert str(OLD_WEBINAR_ID) not in ids
    assert str(CYCLELESS_WEBINAR_ID) not in ids


# ── Send test ────────────────────────────────────────────────────────────────


def test_send_test_sends_to_admin_and_logs(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/send-test", json=_broadcast_payload())
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_to"] == "admin@collegemoneymethod.com"

    db = client._session_local()
    try:
        logs = db.query(EmailSendLog).all()
        assert len(logs) == 1
        assert logs[0].recipient_email == "admin@collegemoneymethod.com"
        assert logs[0].status == "sent"
    finally:
        db.close()


def test_send_test_falls_back_to_login_email_when_no_contact(make_client):
    client = make_client("super_admin")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", email="super@example.com"
    )
    resp = client.post("/api/v1/emails/preview/send-test", json=_broadcast_payload())
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_to"] == "super@example.com"


def test_send_test_returns_400_when_admin_has_no_email(make_client):
    client = make_client("super_admin")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), role="super_admin", email=None
    )
    resp = client.post("/api/v1/emails/preview/send-test", json=_broadcast_payload())
    assert resp.status_code == 400


def test_send_test_uses_the_selected_sender(make_client):
    """The From identity chosen on the compose form must carry into the test
    send — otherwise the test arrives from the configured default and tells the
    admin nothing about how the real broadcast will look."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/send-test",
        json=_broadcast_payload(
            sender_name="News Flash", sender_email="newsflash@collegemoneymethod.com"
        ),
    )
    assert resp.status_code == 200, resp.text
    kwargs = client._mock_ses.send_raw_email.call_args.kwargs
    assert kwargs["Source"] == "News Flash <newsflash@collegemoneymethod.com>"
    assert b"newsflash@collegemoneymethod.com" in kwargs["RawMessage"]["Data"]


def test_send_test_falls_back_to_default_sender_when_none_chosen(make_client):
    client = make_client("super_admin")
    resp = client.post("/api/v1/emails/preview/send-test", json=_broadcast_payload())
    assert resp.status_code == 200, resp.text
    kwargs = client._mock_ses.send_raw_email.call_args.kwargs
    assert "noreply@collegemoneymethod.com" in kwargs["Source"]


def test_send_test_rejects_off_domain_sender(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/send-test",
        json=_broadcast_payload(sender_email="spoof@evil.example"),
    )
    assert resp.status_code == 400
    client._mock_ses.send_raw_email.assert_not_called()


def test_send_test_without_branding_sends_bare_html(make_client):
    """A send-test that did not opt into branding must carry no brand fonts,
    colors, or the CMM shell — it should look like a plain typed message."""
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/send-test", json=_broadcast_payload(include_branding=False)
    )
    assert resp.status_code == 200, resp.text
    html = _sent_html(client)
    assert "font-family" not in html
    assert "Lora" not in html
    # Bare paragraph — not even an empty style attribute.
    assert "<p>Hi " in html
    assert "<p style" not in html


def test_send_test_with_branding_sends_the_cmm_shell(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/preview/send-test", json=_broadcast_payload(include_branding=True)
    )
    assert resp.status_code == 200, resp.text
    html = _sent_html(client)
    assert "font-family" in html
