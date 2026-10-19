"""Shared harness for the Paul booking tests.

In-memory SQLite + TestClient, acting as super_admin. Zoom is always mocked:
``zoom_busy`` is the list the busy lookup returns (set ``zoom_busy.error`` to make
it fail) and ``zoom_create`` records meeting creations. The SES stub from the
root conftest records every raw email, so tests can count what was sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401 - registers every model with Base.metadata
from src.app_config.models import AppConfig
from src.auth.deps import get_current_user
from src.auth.rate_limit import _hits
from src.auth.schemas import CurrentUser
from src.config import settings
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.integrations.zoom import ZoomApiError
from src.main import app
from src.paul_booking import decisions, service

TABLES = (
    "app_config", "guest_contacts", "paul_time_off", "paul_meeting_requests",
    "paul_meeting_request_blocks", "email_send_log", "email_suppression",
)
EVERY_DAY = {d: {"start": "00:00", "end": "23:30"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class ZoomBusy:
    """What Paul's Zoom calendar says; ``error`` makes the lookup fail."""

    def __init__(self) -> None:
        self.intervals: list = []
        self.error: Exception | None = None

    def __call__(self, start, end, fresh=False):
        if self.error:
            raise self.error
        return list(self.intervals)


class ZoomCreate:
    """Stands in for meeting creation; ``error`` makes it fail like Zoom 500."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return "987654321", "https://zoom.example/j/987654321"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "unsubscribe_secret_key", "test-signing-secret")


@pytest.fixture
def zoom_busy(monkeypatch) -> ZoomBusy:
    busy = ZoomBusy()
    monkeypatch.setattr(service, "get_paul_busy", busy)
    return busy


@pytest.fixture
def zoom_create(monkeypatch) -> ZoomCreate:
    create = ZoomCreate()
    monkeypatch.setattr(decisions, "create_paul_meeting", create)
    return create


@pytest.fixture
def client(zoom_busy, zoom_create):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[n] for n in TABLES])
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=ADMIN_ID, role="super_admin", school_id=None)
    _hits.clear()

    c = TestClient(app)
    c._session_local = SessionLocal
    yield c

    app.dependency_overrides.clear()
    _hits.clear()


def set_config(client, **fields) -> None:
    """Create/update the app_config row directly."""
    db = client._session_local()
    try:
        cfg = db.query(AppConfig).first() or AppConfig()
        for k, v in fields.items():
            setattr(cfg, k, v)
        db.add(cfg)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def booking_on(client):
    set_config(client, paul_booking_enabled=True, paul_booking_weekly_hours=EVERY_DAY)
    return client


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def zoom_error(status: int = 500) -> ZoomApiError:
    return ZoomApiError("HTTP 500: boom", status_code=status)


def visitor(email: str = "ana@example.com", **over) -> dict:
    body = {
        "first_name": "Ana", "last_name": "Rivera", "email": email, "school_name": "Lincoln High School",
        "role": "Counselor", "message": "We would love to talk about financial aid nights.",
        "time_zone": "America/Chicago",
    }
    return {**body, **over}


def post_request(client, start: str, duration: int = 30, ip: str = "203.0.113.1", **over):
    body = {**visitor(**{k: v for k, v in over.items() if k in visitor()}), "start": start, "duration": duration}
    body.update({k: v for k, v in over.items() if k not in visitor()})
    return client.post("/api/v1/paul-booking/requests", json=body, headers={"x-forwarded-for": ip})


def first_slots(client, duration: int = 30) -> list[str]:
    resp = client.get("/api/v1/paul-booking/slots", params={"duration": duration})
    assert resp.status_code == 200, resp.text
    return resp.json()["slots"]
