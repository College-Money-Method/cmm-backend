"""HTTP-level tests for the email template management endpoints (super_admin
only). Follows the in-memory SQLite + TestClient + dependency_overrides
pattern from tests/emails/test_broadcast_router.py. Covers full CRUD,
`?category=` filtering, and that `category` is immutable via PATCH.
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
from src.emails.email_template_models import EmailTemplate
from src.main import app

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BROADCAST_TEMPLATE_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
AUTOMATION_TEMPLATE_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

SAMPLE_BODY = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]}


@pytest.fixture
def make_client(monkeypatch):
    """Factory: TestClient acting as `role`, with one seeded template per
    category so `?category=` filtering has something real to narrow down."""

    def _build(role: str):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        tables = [t for n, t in Base.metadata.tables.items() if n == "email_template"]
        Base.metadata.create_all(engine, tables=tables)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        seed = SessionLocal()
        seed.add(
            EmailTemplate(
                id=BROADCAST_TEMPLATE_ID,
                category="general",
                name="Broadcast Template",
                subject="News from CMM",
                body_json='{"type":"doc","content":[]}',
            )
        )
        seed.add(
            EmailTemplate(
                id=AUTOMATION_TEMPLATE_ID,
                category="workshop",
                name="Automation Template",
                subject="Reminder: {{workshop_name}}",
                body_json='{"type":"doc","content":[]}',
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
def test_non_super_admin_cannot_list_templates(make_client, role: str):
    client = make_client(role)
    resp = client.get("/api/v1/emails/templates")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_create_template(make_client, role: str):
    client = make_client(role)
    resp = client.post(
        "/api/v1/emails/templates",
        json={"category": "general", "name": "X", "subject": "X", "body_json": SAMPLE_BODY},
    )
    assert resp.status_code == 403


# ── List / filter ────────────────────────────────────────────────────────────


def test_list_returns_all_templates(make_client):
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/templates")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_filters_by_category(make_client):
    client = make_client("super_admin")
    resp = client.get("/api/v1/emails/templates", params={"category": "workshop"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(AUTOMATION_TEMPLATE_ID)
    assert body[0]["category"] == "workshop"


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_template_succeeds(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/templates",
        json={"category": "general", "name": "New Template", "subject": "Hi", "body_json": SAMPLE_BODY},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Template"
    assert body["category"] == "general"
    assert body["body_json"] == SAMPLE_BODY


def test_create_template_rejects_invalid_category(make_client):
    client = make_client("super_admin")
    resp = client.post(
        "/api/v1/emails/templates",
        json={"category": "not_a_real_category", "name": "X", "subject": "X", "body_json": SAMPLE_BODY},
    )
    assert resp.status_code == 422


# ── Get ──────────────────────────────────────────────────────────────────────


def test_get_template_returns_seeded_row(make_client):
    client = make_client("super_admin")
    resp = client.get(f"/api/v1/emails/templates/{BROADCAST_TEMPLATE_ID}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Broadcast Template"


def test_get_unknown_template_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.get(f"/api/v1/emails/templates/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Patch ────────────────────────────────────────────────────────────────────


def test_patch_updates_name_and_subject(make_client):
    client = make_client("super_admin")
    resp = client.patch(
        f"/api/v1/emails/templates/{BROADCAST_TEMPLATE_ID}",
        json={"name": "Renamed", "subject": "New subject"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["subject"] == "New subject"


def test_patch_category_field_is_ignored_and_category_stays_immutable(make_client):
    client = make_client("super_admin")
    resp = client.patch(
        f"/api/v1/emails/templates/{BROADCAST_TEMPLATE_ID}",
        json={"category": "workshop", "name": "Still Broadcast"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "general"
    assert body["name"] == "Still Broadcast"

    db = client._session_local()
    try:
        template = db.get(EmailTemplate, BROADCAST_TEMPLATE_ID)
        assert template.category == "general"
    finally:
        db.close()


def test_patch_unknown_template_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.patch(f"/api/v1/emails/templates/{uuid.uuid4()}", json={"name": "X"})
    assert resp.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────────────


def test_delete_removes_template(make_client):
    client = make_client("super_admin")
    resp = client.delete(f"/api/v1/emails/templates/{BROADCAST_TEMPLATE_ID}")
    assert resp.status_code == 204

    db = client._session_local()
    try:
        assert db.get(EmailTemplate, BROADCAST_TEMPLATE_ID) is None
    finally:
        db.close()


def test_delete_unknown_template_returns_404(make_client):
    client = make_client("super_admin")
    resp = client.delete(f"/api/v1/emails/templates/{uuid.uuid4()}")
    assert resp.status_code == 404
