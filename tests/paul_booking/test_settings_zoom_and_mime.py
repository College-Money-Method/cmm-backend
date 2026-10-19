"""AppConfig fields, Zoom busy expansion, .ics shape, and the send_email MIME shape."""

from __future__ import annotations

import email
from unittest.mock import MagicMock

import httpx
import pytest

from src.emails import ses_client
from src.integrations import zoom_paul_calendar as cal
from src.integrations.zoom import ZoomApiError
from src.paul_booking.ics import build_ics
from tests.paul_booking.conftest import utc

CONFIG = "/api/v1/app-config"


# ---- AppConfig PATCH / GET ----

def test_config_roundtrip_of_booking_fields(client):
    hours = {"mon": {"start": "09:00", "end": "17:30"}, "sat": None}
    resp = client.patch(CONFIG, json={
        "paul_booking_enabled": True, "paul_booking_timezone": "America/New_York",
        "paul_booking_weekly_hours": hours,
    })
    assert resp.status_code == 200, resp.text
    got = client.get(CONFIG).json()
    assert got["paul_booking_enabled"] is True
    assert got["paul_booking_timezone"] == "America/New_York"
    assert got["paul_booking_weekly_hours"]["mon"] == {"start": "09:00", "end": "17:30"}


@pytest.mark.parametrize("bad", [
    {"paul_booking_weekly_hours": {"mon": {"start": "09:15", "end": "17:00"}}},  # off the grid
    {"paul_booking_weekly_hours": {"mon": {"start": "17:00", "end": "09:00"}}},  # start >= end
    {"paul_booking_weekly_hours": {"funday": {"start": "09:00", "end": "10:00"}}},  # unknown day
    {"paul_booking_weekly_hours": {"mon": {"start": "9am", "end": "10:00"}}},  # not HH:MM
    {"paul_booking_timezone": "Mars/Olympus"},
    {"paul_booking_enabled": None},
    {"paul_booking_notify_email": "not-an-email"},
    {"paul_booking_notify_email": "a@example.com, b@example.com"},
])
def test_config_rejects_invalid_booking_fields(client, bad):
    assert client.patch(CONFIG, json=bad).status_code == 422


def test_config_defaults_are_off_and_unset(client):
    got = client.get(CONFIG).json()
    assert got["paul_booking_enabled"] is False
    assert got["paul_booking_timezone"] is None and got["paul_booking_weekly_hours"] is None
    assert got["paul_booking_notify_email"] is None


def test_config_notify_email_is_trimmed_and_blank_clears_it(client):
    got = client.patch(CONFIG, json={"paul_booking_notify_email": "  ops@collegemoneymethod.com "}).json()
    assert got["paul_booking_notify_email"] == "ops@collegemoneymethod.com"
    assert client.patch(CONFIG, json={"paul_booking_notify_email": ""}).json()["paul_booking_notify_email"] is None


# ---- Zoom busy lookup ----

def _resp(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://api.zoom.us"))


@pytest.fixture
def zoom_http(monkeypatch):
    cal.reset_busy_cache()
    monkeypatch.setattr(cal, "is_configured", lambda: True)
    monkeypatch.setattr(cal, "_get_access_token", lambda: "tok")
    routes: dict[str, httpx.Response] = {}
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return _resp({}, 404)

    monkeypatch.setattr(cal.httpx, "get", fake_get)
    yield routes, calls
    cal.reset_busy_cache()


WINDOW = (utc(2026, 11, 1), utc(2026, 12, 1))


def test_busy_expands_recurring_skips_deleted_and_ignores_no_fixed_time(zoom_http):
    routes, _ = zoom_http
    routes["/meetings"] = _resp({"meetings": [
        {"id": 1, "type": 2, "start_time": "2026-11-03T17:00:00Z", "duration": 30},
        {"id": 2, "type": 8},
        {"id": 3, "type": 3},  # recurring, no fixed time: nothing to block
        {"id": 4, "type": 1, "start_time": "2026-11-04T17:00:00Z", "duration": 30},  # instant
    ]})
    routes["/meetings/2"] = _resp({"occurrences": [
        {"start_time": "2026-11-10T18:00:00Z", "duration": 60},
        {"start_time": "2026-11-17T18:00:00Z", "duration": 60, "status": "deleted"},
        {"start_time": "2026-11-24T18:00:00Z", "duration": 60, "status": "available"},
    ]})
    routes["/webinars"] = _resp({"webinars": [
        {"id": 9, "type": 5, "start_time": "2026-11-05T01:00:00Z", "duration": 90},
        {"id": 10, "type": 9},
    ]})
    routes["/webinars/10"] = _resp({"occurrences": [{"start_time": "2026-11-12T01:00:00Z", "duration": 60}]})

    got = sorted(cal.get_paul_busy(*WINDOW, fresh=True))
    assert got == [
        (utc(2026, 11, 3, 17), utc(2026, 11, 3, 17, 30)),
        (utc(2026, 11, 5, 1), utc(2026, 11, 5, 2, 30)),
        (utc(2026, 11, 10, 18), utc(2026, 11, 10, 19)),
        (utc(2026, 11, 12, 1), utc(2026, 11, 12, 2)),
        (utc(2026, 11, 24, 18), utc(2026, 11, 24, 19)),
    ]


def test_busy_is_cached_and_filtered_per_window(zoom_http):
    routes, calls = zoom_http
    routes["/meetings"] = _resp({"meetings": [
        {"id": 1, "type": 2, "start_time": "2026-11-03T17:00:00Z", "duration": 30},
        {"id": 2, "type": 2, "start_time": "2026-12-20T17:00:00Z", "duration": 30},
    ]})
    routes["/webinars"] = _resp({"webinars": []})
    assert len(cal.get_paul_busy(*WINDOW)) == 1
    fetched = len(calls)
    assert len(cal.get_paul_busy(utc(2026, 12, 15), utc(2026, 12, 30))) == 1  # cache hit, other window
    assert len(calls) == fetched
    cal.get_paul_busy(*WINDOW, fresh=True)
    assert len(calls) > fetched


def test_busy_failure_raises_zoom_error(zoom_http):
    routes, _ = zoom_http
    routes["/meetings"] = _resp({"code": 4711, "message": "Invalid access token, does not contain scopes"}, 401)
    with pytest.raises(ZoomApiError):
        cal.get_paul_busy(*WINDOW, fresh=True)


def test_busy_failure_is_remembered_briefly(zoom_http, monkeypatch):
    routes, calls = zoom_http
    routes["/meetings"] = _resp({"message": "down"}, 503)
    with pytest.raises(ZoomApiError):
        cal.get_paul_busy(*WINDOW)
    asked = len(calls)
    for fresh in (False, True):  # neither the cached nor the fresh path asks Zoom again
        with pytest.raises(ZoomApiError):
            cal.get_paul_busy(*WINDOW, fresh=fresh)
    assert len(calls) == asked

    routes["/meetings"] = _resp({"meetings": []})
    routes["/webinars"] = _resp({"webinars": []})
    real = cal.time.monotonic
    monkeypatch.setattr(cal.time, "monotonic", lambda: real() + cal._FAILURE_TTL_SECONDS + 1)
    assert cal.get_paul_busy(*WINDOW) == []
    assert len(calls) > asked


def test_busy_gives_up_waiting_behind_a_stuck_refresh(zoom_http, monkeypatch):
    monkeypatch.setattr(cal, "_LOCK_WAIT_SECONDS", 0.01)
    assert cal._refresh_lock.acquire()
    try:
        with pytest.raises(ZoomApiError):
            cal.get_paul_busy(*WINDOW)
    finally:
        cal._refresh_lock.release()


def test_busy_not_configured_raises(monkeypatch):
    cal.reset_busy_cache()
    monkeypatch.setattr(cal, "is_configured", lambda: False)
    with pytest.raises(ZoomApiError):
        cal.get_paul_busy(*WINDOW)


# ---- .ics and MIME ----

def _ics(**over) -> str:
    args = dict(
        uid="abc", start=utc(2026, 11, 3, 17), duration_minutes=60,
        summary="Call; with, Paul", description="Line one\nLine two " + "x" * 120,
        location="https://zoom.example/j/1", organizer_email="paul@example.com", now=utc(2026, 10, 15),
    )
    return build_ics(**{**args, **over}).decode("utf-8")


def test_ics_shape():
    text = _ics()
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and "METHOD:PUBLISH" in lines and "UID:abc@collegemoneymethod.com" in lines
    assert "DTSTART:20261103T170000Z" in lines and "DTEND:20261103T180000Z" in lines
    assert r"SUMMARY:Call\; with\, Paul" in lines
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    assert any(line.startswith(" ") for line in lines)  # the long description folded
    assert "Line one\\nLine two" in text.replace("\r\n ", "")


def _db():
    db = MagicMock()
    db.scalar.return_value = None
    return db


def _raw(**kw) -> email.message.Message:
    from src.emails.ses_client import _build_raw_message

    return email.message_from_bytes(_build_raw_message(["a@example.com"], "Hi", "<p>hi</p>", "hi", **kw))


def test_send_email_without_attachments_is_unchanged():
    msg = _raw()
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain", "text/html"]


def test_send_email_with_attachment_wraps_in_mixed():
    msg = _raw(attachments=[("invite.ics", "text/calendar", b"BEGIN:VCALENDAR\r\n")])
    assert msg.get_content_type() == "multipart/mixed"
    body, attachment = msg.get_payload()
    assert body.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in body.get_payload()] == ["text/plain", "text/html"]
    assert attachment.get_content_type() == "text/calendar"
    assert attachment.get_filename() == "invite.ics"
    assert attachment.get_payload(decode=True) == b"BEGIN:VCALENDAR\r\n"
    assert msg["Subject"] == "Hi" and msg["To"] == "a@example.com"


def test_send_email_passes_attachments_through(monkeypatch, _no_real_email):
    monkeypatch.setattr(ses_client, "_sandbox_enabled", lambda db: False)
    ses_client.send_email(_db(), "a@example.com", "Hi", "<p>hi</p>", "hi", "paul_booking",
                          attachments=[("i.ics", "text/calendar", b"x")])
    (call,) = _no_real_email.calls
    assert email.message_from_bytes(call["RawMessage"]["Data"]).get_content_type() == "multipart/mixed"
    _no_real_email.calls.clear()
    ses_client.send_email(_db(), "a@example.com", "Hi", "<p>hi</p>", "hi", "broadcast")
    assert email.message_from_bytes(_no_real_email.calls[0]["RawMessage"]["Data"]).get_content_type() == "multipart/alternative"
