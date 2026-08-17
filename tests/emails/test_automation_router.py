"""HTTP-level tests for the automation management endpoints (super_admin only).

Follows the in-memory SQLite + TestClient + dependency_overrides pattern from
tests/emails/test_broadcast_router.py. Covers the full CRUD surface added on
top of the dynamic-offset schema: create/list/patch/delete, validation
(offset_value>0, enum literals), and `sent_count` derived from `automation_id`
on `EmailSendLog` (isolated per automation).
"""

from __future__ import annotations

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
from src.emails.automation_models import EmailAutomation
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.main import app

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTOMATION_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_AUTOMATION_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-cccccccccccc")


@pytest.fixture
def make_client(monkeypatch):
    """Factory: TestClient acting as `role`, with one seeded (disabled)
    pre_workshop_reminder automation, a second automation, and send-log rows
    scoped to each via `automation_id` (to prove `sent_count` isolation)."""

    def _build(role: str):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        tables = [
            t
            for n, t in Base.metadata.tables.items()
            if n in ("email_automation", "email_send_log", "email_template")
        ]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(
            EmailAutomation(
                id=AUTOMATION_ID,
                name="Pre-Workshop Reminder",
                type="pre_workshop_reminder",
                enabled=False,
                offset_value=7,
                offset_unit="days",
                offset_direction="before",
            )
        )
        seed.add(
            EmailAutomation(
                id=OTHER_AUTOMATION_ID,
                name="Post-Workshop Follow-up",
                type="post_workshop_reminder",
                enabled=True,
                offset_value=2,
                offset_unit="days",
                offset_direction="after",
            )
        )
        for i in range(2):
            seed.add(
                EmailSendLog(
                    id=uuid.uuid4(),
                    recipient_email=f"family{i}@example.com",
                    subject="Reminder",
                    status="sent",
                    source="pre_workshop",
                    automation_id=AUTOMATION_ID,
                )
            )
        seed.add(
            EmailSendLog(
                id=uuid.uuid4(),
                recipient_email="other@example.com",
                subject="Follow-up",
                status="sent",
                source="post_workshop",
                automation_id=OTHER_AUTOMATION_ID,
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
            user_id=ADMIN_USER_ID, role=role, school_id=None
        )

        client = TestClient(app)
        client._session_local = SessionLocal
        return client

    yield _build
    app.dependency_overrides.clear()


# ── Authorization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_list_automations(make_client, role: str):
    client = make_client(role)
    resp = client.get("/api/v1/emails/automations")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_create_automation(make_client, role: str):
    client = make_client(role)
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "New Automation",
            "type": "pre_workshop_reminder",
            "offset_value": 5,
            "offset_unit": "days",
            "offset_direction": "before",
        },
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_patch_automation(make_client, role: str):
    client = make_client(role)
    resp = client.patch(f"/api/v1/emails/automations/{AUTOMATION_ID}", json={"enabled": True})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_delete_automation(make_client, role: str):
    client = make_client(role)
    resp = client.delete(f"/api/v1/emails/automations/{AUTOMATION_ID}")
    assert resp.status_code == 403


# ── List ─────────────────────────────────────────────────────────────────────


def test_super_admin_lists_automations_with_isolated_sent_count(make_client):
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/automations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2

    by_id = {row["id"]: row for row in body}
    assert by_id[str(AUTOMATION_ID)]["type"] == "pre_workshop_reminder"
    assert by_id[str(AUTOMATION_ID)]["enabled"] is False
    assert by_id[str(AUTOMATION_ID)]["offset_value"] == 7
    assert by_id[str(AUTOMATION_ID)]["sent_count"] == 2

    assert by_id[str(OTHER_AUTOMATION_ID)]["type"] == "post_workshop_reminder"
    assert by_id[str(OTHER_AUTOMATION_ID)]["sent_count"] == 1


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_automation_succeeds_with_valid_payload(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "New Automation",
            "type": "post_workshop_reminder",
            "offset_value": 5,
            "offset_unit": "hours",
            "offset_direction": "after",
            "enabled": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Automation"
    assert body["offset_unit"] == "hours"
    assert body["sent_count"] == 0


def _seed_template(client: TestClient, category: str) -> uuid.UUID:
    template_id = uuid.uuid4()
    db = client._session_local()
    try:
        db.add(
            EmailTemplate(
                id=template_id,
                category=category,
                name=f"{category} template",
                subject="Hi",
                body_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()
    return template_id


def test_create_automation_accepts_valid_workshop_template(make_client):
    client = make_client("super_admin")
    template_id = _seed_template(client, "workshop")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "With Template",
            "type": "pre_workshop_reminder",
            "offset_value": 3,
            "offset_unit": "days",
            "offset_direction": "before",
            "template_id": str(template_id),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["template_id"] == str(template_id)


def test_create_automation_rejects_unknown_template_id(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "Dangling Template",
            "type": "pre_workshop_reminder",
            "offset_value": 3,
            "offset_unit": "days",
            "offset_direction": "before",
            "template_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422


def test_create_automation_rejects_wrong_category_template(make_client):
    client = make_client("super_admin")
    template_id = _seed_template(client, "general")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "Wrong Category",
            "type": "pre_workshop_reminder",
            "offset_value": 3,
            "offset_unit": "days",
            "offset_direction": "before",
            "template_id": str(template_id),
        },
    )
    assert resp.status_code == 422


def test_patch_rejects_unknown_template_id(make_client):
    client = make_client("super_admin")
    resp = client.patch(
        f"/api/v1/emails/automations/{AUTOMATION_ID}",
        json={"template_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_create_automation_rejects_non_positive_offset_value(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "Bad Offset",
            "type": "pre_workshop_reminder",
            "offset_value": 0,
            "offset_unit": "days",
            "offset_direction": "before",
        },
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "mid_workshop_reminder"),
        ("offset_unit", "weeks"),
        ("offset_direction", "sideways"),
    ],
)
def test_create_automation_rejects_invalid_enum_values(make_client, field: str, value: str):
    client = make_client("super_admin")
    payload = {
        "name": "Bad Enum",
        "type": "pre_workshop_reminder",
        "offset_value": 5,
        "offset_unit": "days",
        "offset_direction": "before",
    }
    payload[field] = value
    resp = client.post("/api/v1/emails/automations", json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "automation_type,bad_direction",
    [
        ("pre_workshop_reminder", "after"),
        ("post_workshop_reminder", "before"),
    ],
)
def test_create_automation_rejects_direction_mismatched_with_type(
    make_client, automation_type: str, bad_direction: str
):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/automations",
        json={
            "name": "Mismatched Direction",
            "type": automation_type,
            "offset_value": 3,
            "offset_unit": "days",
            "offset_direction": bad_direction,
        },
    )
    assert resp.status_code == 422


def test_patch_type_without_direction_rejected_when_inconsistent(make_client):
    # AUTOMATION_ID is a pre_workshop_reminder/before row; switching only the
    # type to post_workshop_reminder would leave direction "before" — invalid.
    client = make_client("super_admin")
    resp = client.patch(
        f"/api/v1/emails/automations/{AUTOMATION_ID}",
        json={"type": "post_workshop_reminder"},
    )
    assert resp.status_code == 422


def test_patch_type_with_matching_direction_succeeds(make_client):
    client = make_client("super_admin")
    resp = client.patch(
        f"/api/v1/emails/automations/{AUTOMATION_ID}",
        json={"type": "post_workshop_reminder", "offset_direction": "after"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "post_workshop_reminder"
    assert body["offset_direction"] == "after"


# ── Patch ────────────────────────────────────────────────────────────────────


def test_patch_toggles_enabled_and_persists(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/automations/{AUTOMATION_ID}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    db = client._session_local()
    try:
        automation = db.get(EmailAutomation, AUTOMATION_ID)
        assert automation.enabled is True
    finally:
        db.close()


def test_patch_updates_offset_value_and_persists(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/automations/{AUTOMATION_ID}", json={"offset_value": 3})
    assert resp.status_code == 200
    assert resp.json()["offset_value"] == 3

    db = client._session_local()
    try:
        automation = db.get(EmailAutomation, AUTOMATION_ID)
        assert automation.offset_value == 3
    finally:
        db.close()


def test_patch_rejects_non_positive_offset_value(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/automations/{AUTOMATION_ID}", json={"offset_value": 0})
    assert resp.status_code == 422


def test_patch_rejects_invalid_offset_unit(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/automations/{AUTOMATION_ID}", json={"offset_unit": "weeks"})
    assert resp.status_code == 422


def test_patch_unknown_automation_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/automations/{uuid.uuid4()}", json={"enabled": True})
    assert resp.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────────────


def test_delete_removes_automation(make_client):
    client = make_client("super_admin")
    resp = client.delete(f"/api/v1/emails/automations/{AUTOMATION_ID}")
    assert resp.status_code == 204

    db = client._session_local()
    try:
        assert db.get(EmailAutomation, AUTOMATION_ID) is None
    finally:
        db.close()


def test_delete_unknown_automation_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.delete(f"/api/v1/emails/automations/{uuid.uuid4()}")
    assert resp.status_code == 404
