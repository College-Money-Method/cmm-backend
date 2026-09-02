"""A webinar cannot be rescheduled into the past by accident.

`PATCH /webinars/{id}` is the only way an admin moves a session, and the
automations hang off `start_datetime`: reminders fire on offsets from it and
follow-ups fire after it. So a mistyped year or AM/PM does not just show the
wrong date — it can make a future session look finished (follow-ups go out) or
a finished one look upcoming. The endpoint therefore refuses a past start
unless the admin says explicitly that they are correcting a historical record,
and it records a *material* future move as a reschedule so the automation
re-arm has something unambiguous to read.

The two must not be confused: a correction leaves the trail alone (nothing
moved, so nobody is re-emailed), a real move writes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.workshops.models import Webinar, Workshop

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PAST_WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
FUTURE_WEBINAR_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
UNSCHEDULED_WEBINAR_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

# Relative to real "now", because the endpoint compares against it.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PAST_START = NOW - timedelta(days=14)
FUTURE_START = NOW + timedelta(days=14)


@pytest.fixture
def client(webinar_sessionmaker):
    """Admin client over one finished session, one upcoming, one never scheduled."""
    seed = webinar_sessionmaker()
    seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    seed.flush()
    seed.add(
        Webinar(
            id=PAST_WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            webinar_name="Already happened",
            start_datetime=PAST_START,
            end_datetime=PAST_START + timedelta(hours=1),
        )
    )
    seed.add(
        Webinar(
            id=FUTURE_WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            webinar_name="Upcoming",
            start_datetime=FUTURE_START,
            end_datetime=FUTURE_START + timedelta(hours=1),
        )
    )
    seed.add(
        Webinar(
            id=UNSCHEDULED_WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            webinar_name="Not scheduled yet",
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
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, email="admin@collegemoneymethod.com", role="super_admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _utc(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _patch(client: TestClient, webinar_id: uuid.UUID, body: dict):
    return client.patch(f"/api/v1/workshops/webinars/{webinar_id}", json=body)


def _stored(sessionmaker_, webinar_id: uuid.UUID) -> SimpleNamespace:
    """The webinar's schedule as stored, with UTC re-attached.

    Postgres keeps the offset on these TIMESTAMPTZ columns; the SQLite test
    engine drops it and hands back naive datetimes, which cannot be compared to
    the aware ones the assertions use.
    """
    db = sessionmaker_()
    try:
        obj = db.get(Webinar, webinar_id)
        return SimpleNamespace(
            start_datetime=_utc(obj.start_datetime),
            end_datetime=_utc(obj.end_datetime),
            previous_start_datetime=_utc(obj.previous_start_datetime),
            rescheduled_at=_utc(obj.rescheduled_at),
        )
    finally:
        db.close()


class TestPastStartGuard:
    def test_moving_a_session_into_the_past_is_rejected(self, client, webinar_sessionmaker):
        """The typo case: an upcoming session dated into last month."""
        resp = _patch(client, FUTURE_WEBINAR_ID, {"start_datetime": (NOW - timedelta(days=30)).isoformat()})
        assert resp.status_code == 422
        assert "already in the past" in resp.json()["detail"]

        stored = _stored(webinar_sessionmaker, FUTURE_WEBINAR_ID)
        assert stored.start_datetime == FUTURE_START, "a refused patch must not move the session"

    def test_override_saves_the_past_start_without_claiming_a_reschedule(self, client, webinar_sessionmaker):
        """The admin is correcting a recorded date, not announcing a new one."""
        corrected = NOW - timedelta(days=30)
        resp = _patch(
            client,
            FUTURE_WEBINAR_ID,
            {
                "start_datetime": corrected.isoformat(),
                "end_datetime": (corrected + timedelta(hours=1)).isoformat(),
                "allow_past_datetime": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["previous_start_datetime"] is None
        assert resp.json()["rescheduled_at"] is None

        stored = _stored(webinar_sessionmaker, FUTURE_WEBINAR_ID)
        assert stored.start_datetime == corrected
        assert stored.previous_start_datetime is None, "a correction is not a move; nobody gets re-emailed"
        assert stored.rescheduled_at is None

    def test_editing_a_past_session_without_touching_the_date_is_allowed(self, client):
        """The guard is about the datetime, not about the session being old."""
        resp = _patch(client, PAST_WEBINAR_ID, {"video_embed_code": "<iframe src='recording'></iframe>"})
        assert resp.status_code == 200
        assert resp.json()["video_embed_code"] == "<iframe src='recording'></iframe>"

    def test_resending_the_same_past_start_is_allowed(self, client):
        """A form that round-trips every field must not fail on an unchanged date."""
        resp = _patch(client, PAST_WEBINAR_ID, {"start_datetime": PAST_START.isoformat(), "webinar_name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["webinar_name"] == "Renamed"


class TestInvertedRange:
    def test_end_before_start_is_rejected(self, client, webinar_sessionmaker):
        """`duration_minutes` is a generated column: an inverted pair stores a
        negative duration instead of failing, so it has to be caught here."""
        resp = _patch(
            client,
            FUTURE_WEBINAR_ID,
            {"end_datetime": (FUTURE_START - timedelta(hours=1)).isoformat()},
        )
        assert resp.status_code == 422
        assert "end time must be after" in resp.json()["detail"]

        stored = _stored(webinar_sessionmaker, FUTURE_WEBINAR_ID)
        assert stored.end_datetime == FUTURE_START + timedelta(hours=1)

    def test_zero_length_session_is_rejected(self, client):
        resp = _patch(client, FUTURE_WEBINAR_ID, {"end_datetime": FUTURE_START.isoformat()})
        assert resp.status_code == 422


class TestRescheduleTrail:
    def test_material_future_move_is_recorded(self, client, webinar_sessionmaker):
        new_start = FUTURE_START + timedelta(days=7)
        resp = _patch(
            client,
            FUTURE_WEBINAR_ID,
            {
                "start_datetime": new_start.isoformat(),
                "end_datetime": (new_start + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["previous_start_datetime"] is not None
        assert resp.json()["rescheduled_at"] is not None

        stored = _stored(webinar_sessionmaker, FUTURE_WEBINAR_ID)
        assert stored.start_datetime == new_start
        assert stored.previous_start_datetime == FUTURE_START, "the trail must name where it used to start"
        assert stored.rescheduled_at is not None

    def test_finished_session_moved_to_a_future_date_is_a_reschedule(self, client, webinar_sessionmaker):
        """Reviving a session that already happened: the date genuinely moved,
        so its automations are stale and must be treated as re-armable."""
        new_start = NOW + timedelta(days=3)
        resp = _patch(
            client,
            PAST_WEBINAR_ID,
            {
                "start_datetime": new_start.isoformat(),
                "end_datetime": (new_start + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.status_code == 200

        stored = _stored(webinar_sessionmaker, PAST_WEBINAR_ID)
        assert stored.previous_start_datetime == PAST_START
        assert stored.rescheduled_at is not None

    def test_small_nudge_is_not_a_reschedule(self, client, webinar_sessionmaker):
        """A half-hour correction is not worth re-emailing every counselor over."""
        nudged = FUTURE_START + timedelta(minutes=30)
        resp = _patch(client, FUTURE_WEBINAR_ID, {"start_datetime": nudged.isoformat()})
        assert resp.status_code == 200

        stored = _stored(webinar_sessionmaker, FUTURE_WEBINAR_ID)
        assert stored.start_datetime == nudged
        assert stored.previous_start_datetime is None
        assert stored.rescheduled_at is None

    def test_first_time_scheduling_is_not_a_reschedule(self, client, webinar_sessionmaker):
        """Nothing was announced before, so there is nothing to re-announce."""
        start = NOW + timedelta(days=21)
        resp = _patch(
            client,
            UNSCHEDULED_WEBINAR_ID,
            {"start_datetime": start.isoformat(), "end_datetime": (start + timedelta(hours=1)).isoformat()},
        )
        assert resp.status_code == 200

        stored = _stored(webinar_sessionmaker, UNSCHEDULED_WEBINAR_ID)
        assert stored.start_datetime == start
        assert stored.previous_start_datetime is None
        assert stored.rescheduled_at is None

    def test_override_of_a_material_move_still_records_nothing(self, client, webinar_sessionmaker):
        """`allow_past_datetime` declares intent, so it suppresses the trail even
        when the dates alone would look like a big move."""
        corrected = PAST_START - timedelta(days=60)
        resp = _patch(
            client,
            PAST_WEBINAR_ID,
            {
                "start_datetime": corrected.isoformat(),
                "end_datetime": (corrected + timedelta(hours=1)).isoformat(),
                "allow_past_datetime": True,
            },
        )
        assert resp.status_code == 200

        stored = _stored(webinar_sessionmaker, PAST_WEBINAR_ID)
        assert stored.start_datetime == corrected
        assert stored.previous_start_datetime is None
        assert stored.rescheduled_at is None
