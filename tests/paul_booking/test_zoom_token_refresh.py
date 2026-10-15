"""Zoom calls recover from a token issued before Paul granted a scope, Paul
without a webinar licence still gets slots, and the meeting create sends the
body we promise."""

from __future__ import annotations

import httpx
import pytest

from src.integrations import zoom_paul_calendar as cal
from src.integrations import zoom_paul_meetings as meetings
from src.integrations.zoom import ZoomApiError
from tests.paul_booking.conftest import utc

WINDOW = (utc(2026, 11, 1), utc(2026, 12, 1))
NO_SCOPE = {"code": 4711, "message": "Invalid access token, does not contain scopes:[meeting:read:list_meetings:admin]."}


def _resp(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://api.zoom.us"))


class Tokens:
    """Hands out tok0 until forgotten, then tok1: the token Zoom would issue after the scope change."""

    def __init__(self) -> None:
        self.forgotten = 0

    def get(self) -> str:
        return f"tok{self.forgotten}"

    def forget(self) -> None:
        self.forgotten += 1


@pytest.fixture
def tokens(monkeypatch) -> Tokens:
    t = Tokens()
    for module in (cal, meetings):
        monkeypatch.setattr(module, "is_configured", lambda: True)
        monkeypatch.setattr(module, "_get_access_token", t.get)
        monkeypatch.setattr(module, "forget_access_token", t.forget)
    return t


@pytest.fixture
def zoom_get(monkeypatch, tokens):
    """Routes GETs by URL suffix; a route may be a function of the bearer token."""
    cal.reset_busy_cache()
    routes: dict[str, object] = {}
    calls: list[str] = []

    def fake_get(url, headers=None, **kw):
        calls.append(url)
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        for suffix, route in routes.items():
            if url.endswith(suffix):
                return route(token) if callable(route) else route
        return _resp({}, 404)

    monkeypatch.setattr(cal.httpx, "get", fake_get)
    yield routes, calls
    cal.reset_busy_cache()


def _meetings(token: str) -> httpx.Response:
    if token == "tok0":
        return _resp(NO_SCOPE, 400)
    return _resp({"meetings": [{"id": 1, "type": 2, "start_time": "2026-11-03T17:00:00Z", "duration": 30}]})


def test_busy_retries_once_with_a_fresh_token_after_a_scope_refusal(zoom_get, tokens):
    routes, _ = zoom_get
    routes["/meetings"] = _meetings
    routes["/webinars"] = _resp({"webinars": []})
    assert cal.get_paul_busy(*WINDOW, fresh=True) == [(utc(2026, 11, 3, 17), utc(2026, 11, 3, 17, 30))]
    assert tokens.forgotten == 1


def test_busy_still_fails_when_the_fresh_token_lacks_the_scope_too(zoom_get, tokens):
    routes, _ = zoom_get
    routes["/meetings"] = _resp(NO_SCOPE, 400)
    with pytest.raises(ZoomApiError) as exc:
        cal.get_paul_busy(*WINDOW, fresh=True)
    assert exc.value.code == 4711 and tokens.forgotten == 1


def test_busy_does_not_retry_an_outage(zoom_get, tokens):
    routes, calls = zoom_get
    routes["/meetings"] = _resp({"message": "down"}, 503)
    with pytest.raises(ZoomApiError):
        cal.get_paul_busy(*WINDOW, fresh=True)
    assert tokens.forgotten == 0 and len(calls) == 1


def test_busy_without_a_webinar_plan_counts_meetings_only(zoom_get):
    routes, _ = zoom_get
    routes["/meetings"] = _resp({"meetings": [{"id": 1, "type": 2, "start_time": "2026-11-03T17:00:00Z", "duration": 30}]})
    routes["/webinars"] = _resp({"code": 200, "message": "Webinar plan is missing."}, 400)
    assert len(cal.get_paul_busy(*WINDOW, fresh=True)) == 1


@pytest.mark.parametrize("refusal", [
    ({"code": 1001, "message": "User does not exist."}, 404),
    ({"code": 200, "message": "No permission."}, 400),  # code 200 without the plan wording
])
def test_busy_other_webinar_refusals_still_fail(zoom_get, refusal):
    routes, _ = zoom_get
    routes["/meetings"] = _resp({"meetings": []})
    routes["/webinars"] = _resp(*refusal)
    with pytest.raises(ZoomApiError):
        cal.get_paul_busy(*WINDOW, fresh=True)


def test_busy_expands_a_repeated_recurring_series_once(zoom_get):
    routes, calls = zoom_get
    routes["/meetings"] = _resp({"meetings": [
        {"id": 7, "type": 8, "start_time": "2026-11-10T18:00:00Z"},
        {"id": 7, "type": 8, "start_time": "2026-11-17T18:00:00Z"},
    ]})
    routes["/meetings/7"] = _resp({"occurrences": [
        {"start_time": "2026-11-10T18:00:00Z", "duration": 60},
        {"start_time": "2026-11-17T18:00:00Z", "duration": 60},
    ]})
    routes["/webinars"] = _resp({"webinars": []})
    assert len(cal.get_paul_busy(*WINDOW, fresh=True)) == 2
    assert sum(url.endswith("/meetings/7") for url in calls) == 1


# ---- meeting create ----

@pytest.fixture
def zoom_post(monkeypatch, tokens):
    sent: list[tuple[str, dict]] = []

    def fake_post(url, headers=None, json=None, **kw):
        token = headers["Authorization"].removeprefix("Bearer ")
        sent.append((token, json))
        if token == "tok0":
            return _resp({"code": 4711, "message": "Invalid access token, does not contain scopes:[meeting:write:meeting:admin]."}, 400)
        return _resp({"id": 98765, "join_url": "https://zoom.us/j/98765"}, 201)

    monkeypatch.setattr(meetings.httpx, "post", fake_post)
    return sent


def _create(**over):
    args = {"topic": "College Money Method x Lincoln High", "agenda": "Hi", "start_at": utc(2026, 11, 3, 17),
            "duration_minutes": 60, "timezone_name": "America/New_York"}
    return meetings.create_paul_meeting(**{**args, **over})


def test_create_retries_once_with_a_fresh_token_after_a_scope_refusal(zoom_post, tokens):
    assert _create() == ("98765", "https://zoom.us/j/98765")
    assert [token for token, _ in zoom_post] == ["tok0", "tok1"] and tokens.forgotten == 1
    body = zoom_post[-1][1]
    assert body["start_time"] == "2026-11-03T17:00:00Z" and body["duration"] == 60 and body["type"] == 2
    assert body["settings"] == {"waiting_room": True, "join_before_host": False}
    assert body["timezone"] == "America/New_York" and body["agenda"] == "Hi"


def test_create_does_not_retry_a_refused_body(monkeypatch, tokens):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _resp({"code": 300, "message": "Invalid parameter."}, 400)

    monkeypatch.setattr(meetings.httpx, "post", fake_post)
    with pytest.raises(ZoomApiError) as exc:
        _create()
    assert exc.value.code == 300 and len(calls) == 1 and tokens.forgotten == 0
