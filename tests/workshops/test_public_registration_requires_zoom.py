"""A public registration only succeeds once Zoom has the parent.

Zoom sends the join link and the confirmation email. When the Zoom call failed,
the old endpoint had already committed the row, so the parent saw "You're
registered!" and then heard nothing — 16% of registrations on upcoming webinars
ended up like that. Now a Zoom failure fails the request with nothing saved, and
a success hands back the parent's personal join link so the portal can show it
right away, which matters most to someone registering minutes before the start.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.db.client import get_supabase
from src.db.deps import get_db
from src.integrations import zoom
from src.main import app
from src.schools.models import School
from src.workshops.models import Webinar, Workshop, WorkshopRegistration

WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ZOOM_WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
PLAIN_WEBINAR_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SCHOOL_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
JOIN_URL = "https://us06web.zoom.us/w/81276546458?tk=personal"
START = datetime.now(tz=timezone.utc) + timedelta(minutes=10)


@pytest.fixture
def client(webinar_sessionmaker):
    seed = webinar_sessionmaker()
    seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    seed.add(School(id=SCHOOL_ID, name="Annie Wright Schools", slug="annie-wright-schools"))
    seed.flush()
    for webinar_id, zoom_id in ((ZOOM_WEBINAR_ID, "81276546458"), (PLAIN_WEBINAR_ID, None)):
        seed.add(
            Webinar(
                id=webinar_id,
                workshop_id=WORKSHOP_ID,
                webinar_name="Starting soon",
                zoom_webinar_id=zoom_id,
                start_datetime=START,
                end_datetime=START + timedelta(hours=1),
            )
        )
    seed.commit()
    seed.close()

    def override_get_db():
        db = webinar_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def zoom_calls(monkeypatch):
    """Zoom configured; each call records its email and answers from `outcome`."""
    calls: list[str] = []
    outcome: dict[str, zoom.ZoomRegistrant | None] = {
        "result": zoom.ZoomRegistrant("registrant-1", JOIN_URL)
    }

    def fake_register(**kwargs):
        calls.append(kwargs["email"])
        return outcome["result"]

    monkeypatch.setattr(zoom, "is_configured", lambda: True)
    monkeypatch.setattr(zoom, "register_webinar", fake_register)
    return calls, outcome


def _register(client, webinar_id=ZOOM_WEBINAR_ID, email="parent@example.com"):
    return client.post(
        f"/api/v1/workshops/public/webinars/{webinar_id}/register",
        json={"email": email, "first_name": "Ana", "last_name": "Lee", "school_id": str(SCHOOL_ID), "grade": "12th"},
    )


def _rows(sessionmaker_, webinar_id=ZOOM_WEBINAR_ID) -> list[WorkshopRegistration]:
    db = sessionmaker_()
    try:
        return list(db.query(WorkshopRegistration).filter_by(webinar_id=webinar_id))
    finally:
        db.close()


def test_success_returns_the_personal_join_link(client, zoom_calls, webinar_sessionmaker):
    resp = _register(client)
    assert resp.status_code == 201
    assert resp.json()["zoom_join_url"] == JOIN_URL
    assert resp.json()["zoom_registrant_id"] == "registrant-1"
    [row] = _rows(webinar_sessionmaker)
    assert row.zoom_join_url == JOIN_URL


def test_zoom_failure_fails_the_registration_and_saves_nothing(client, zoom_calls, webinar_sessionmaker):
    _calls, outcome = zoom_calls
    outcome["result"] = None
    resp = _register(client)
    assert resp.status_code == 502
    assert "not registered" in resp.json()["detail"]
    assert _rows(webinar_sessionmaker) == []


def test_registering_again_returns_the_link_without_calling_zoom(client, zoom_calls):
    calls, _outcome = zoom_calls
    _register(client)
    again = _register(client)
    assert again.status_code == 201
    assert again.json()["zoom_join_url"] == JOIN_URL
    assert calls == ["parent@example.com"]


def test_a_stranded_registration_is_retried_on_zoom(client, zoom_calls, webinar_sessionmaker):
    """Rows saved before failures were fatal have no registrant; registering again repairs them."""
    seed = webinar_sessionmaker()
    seed.add(WorkshopRegistration(webinar_id=ZOOM_WEBINAR_ID, email="parent@example.com"))
    seed.commit()
    seed.close()

    resp = _register(client)
    assert resp.status_code == 201
    assert resp.json()["zoom_join_url"] == JOIN_URL
    [row] = _rows(webinar_sessionmaker)
    assert row.zoom_registrant_id == "registrant-1"


def test_a_webinar_without_zoom_registers_without_calling_zoom(client, zoom_calls, webinar_sessionmaker):
    calls, _outcome = zoom_calls
    resp = _register(client, webinar_id=PLAIN_WEBINAR_ID)
    assert resp.status_code == 201
    assert resp.json()["zoom_join_url"] is None
    assert calls == []
    assert len(_rows(webinar_sessionmaker, PLAIN_WEBINAR_ID)) == 1


def test_an_install_without_zoom_credentials_still_registers(client, monkeypatch, webinar_sessionmaker):
    """Local and preview installs have no Zoom credentials; registration must keep working there."""
    monkeypatch.setattr(zoom, "is_configured", lambda: False)
    monkeypatch.setattr(zoom, "register_webinar", lambda **_: pytest.fail("must not call Zoom"))
    resp = _register(client)
    assert resp.status_code == 201
    assert len(_rows(webinar_sessionmaker)) == 1
