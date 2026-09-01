"""Tests for GET /api/v1/emails/automations/{id}/sends — the per-recipient send
log behind an automation's `sent_count`.

Uses the shared `scheduler_sessionmaker` fixture (tests/emails/conftest.py)
because this endpoint joins `email_send_log` through `webinars` -> `workshops`
and `schools`, which the CRUD-only fixture in test_automation_router.py does
not create.

The cycle filter is the behavior worth pinning: it must scope through
`webinars.cycle_id`, NOT through `sent_at`. A pre-workshop reminder fires days
before its workshop, so a `sent_at` range would misfile every send near a cycle
boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.cycles.models import Cycle
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.automation_models import EmailAutomation
from src.emails.models import EmailSendLog
from src.main import app
from src.schools.models import School
from src.workshops.models import Webinar, Workshop

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTOMATION_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CURRENT_CYCLE_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
PAST_CYCLE_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

BASE = f"/api/v1/emails/automations/{AUTOMATION_ID}/sends"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(scheduler_sessionmaker):
    """Admin TestClient over a DB holding one automation with 3 sends: two for a
    current-cycle webinar, one for a past-cycle webinar, plus one legacy row
    with no workshop context at all."""
    SessionLocal = scheduler_sessionmaker
    seed = SessionLocal()

    seed.add_all(
        [
            Cycle(id=CURRENT_CYCLE_ID, name="2026-2027", is_current=True),
            Cycle(id=PAST_CYCLE_ID, name="2025-2026", is_current=False),
        ]
    )
    school = School(id=uuid.uuid4(), name="Hampton School")
    workshop = Workshop(id=uuid.uuid4(), name="College Pricing")
    seed.add_all([school, workshop])
    current_webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        cycle_id=CURRENT_CYCLE_ID,
        start_datetime=NOW + timedelta(days=7),
    )
    past_webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        cycle_id=PAST_CYCLE_ID,
        start_datetime=NOW - timedelta(days=300),
    )
    seed.add_all([current_webinar, past_webinar])
    seed.add(
        EmailAutomation(
            id=AUTOMATION_ID,
            name="Pre-Workshop Reminder",
            type="pre_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="before",
        )
    )

    def log(email: str, *, webinar, school_id, sent_at, status="sent") -> EmailSendLog:
        return EmailSendLog(
            id=uuid.uuid4(),
            recipient_email=email,
            subject="Reminder",
            status=status,
            source="pre_workshop",
            automation_id=AUTOMATION_ID,
            webinar_id=webinar.id if webinar else None,
            school_id=school_id,
            sent_at=sent_at,
        )

    seed.add_all(
        [
            log("newest@example.com", webinar=current_webinar, school_id=school.id, sent_at=NOW),
            log(
                "older@example.com",
                webinar=current_webinar,
                school_id=school.id,
                sent_at=NOW - timedelta(hours=1),
                status="failed",
            ),
            log(
                "lastyear@example.com",
                webinar=past_webinar,
                school_id=school.id,
                sent_at=NOW - timedelta(days=300),
            ),
            # Pre-migration row: no webinar/school to attribute it to.
            log("legacy@example.com", webinar=None, school_id=None, sent_at=NOW - timedelta(days=1)),
        ]
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
        user_id=ADMIN_USER_ID, role="super_admin", school_id=None
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lists_every_send_newest_first_without_a_cycle_filter(client):
    body = client.get(BASE).json()
    assert [r["recipient_email"] for r in body["rows"]] == [
        "newest@example.com",
        "older@example.com",
        "legacy@example.com",
        "lastyear@example.com",
    ]
    assert body["has_more"] is False


def test_cycle_filter_scopes_through_the_webinar(client):
    body = client.get(BASE, params={"cycle_id": str(CURRENT_CYCLE_ID)}).json()
    assert [r["recipient_email"] for r in body["rows"]] == [
        "newest@example.com",
        "older@example.com",
    ]


def test_rows_carry_school_and_workshop_names(client):
    rows = client.get(BASE, params={"cycle_id": str(CURRENT_CYCLE_ID)}).json()["rows"]
    assert rows[0]["school_name"] == "Hampton School"
    assert rows[0]["workshop_name"] == "College Pricing"


def test_rows_without_workshop_context_are_listed_but_never_guessed_into_a_cycle(client):
    legacy = next(
        r for r in client.get(BASE).json()["rows"] if r["recipient_email"] == "legacy@example.com"
    )
    assert legacy["school_name"] is None and legacy["workshop_name"] is None
    in_cycle = client.get(BASE, params={"cycle_id": str(CURRENT_CYCLE_ID)}).json()["rows"]
    assert "legacy@example.com" not in [r["recipient_email"] for r in in_cycle]


def test_non_sent_statuses_are_listed_so_failures_are_visible(client):
    rows = client.get(BASE, params={"cycle_id": str(CURRENT_CYCLE_ID)}).json()["rows"]
    assert {r["status"] for r in rows} == {"sent", "failed"}


def test_paginates_with_has_more_and_a_stable_offset(client):
    first = client.get(BASE, params={"limit": 2}).json()
    assert first["has_more"] is True
    assert [r["recipient_email"] for r in first["rows"]] == [
        "newest@example.com",
        "older@example.com",
    ]
    second = client.get(BASE, params={"limit": 2, "offset": 2}).json()
    assert second["has_more"] is False
    assert [r["recipient_email"] for r in second["rows"]] == [
        "legacy@example.com",
        "lastyear@example.com",
    ]


def test_unknown_automation_returns_404(client):
    resp = client.get(f"/api/v1/emails/automations/{uuid.uuid4()}/sends")
    assert resp.status_code == 404


@pytest.mark.parametrize("role", ["hub_admin", "hub_user", "viewer"])
def test_non_super_admin_cannot_read_the_send_log(client, role: str):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, role=role, school_id=None
    )
    assert client.get(BASE).status_code == 403
