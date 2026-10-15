"""Paul's busy time from Zoom: scheduled meetings and webinars.

Kept apart from ``zoom.py`` (already large); reuses its auth and base URL. Needs
the S2S app scopes to list a user's meetings and webinars. Anything that stops us
knowing what Paul is doing raises ``ZoomApiError`` so the booking flow can refuse
to offer slots instead of offering ones that may clash.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from src.config import settings
from src.integrations.zoom import (
    _ZOOM_API_BASE,
    ZoomApiError,
    _get_access_token,
    _zoom_refusal,
    is_configured,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_MAX_PAGES = 10
_CACHE_TTL_SECONDS = 60.0
# A failed lookup is remembered this long, so a slow or down Zoom is asked once
# per window instead of once per visitor.
_FAILURE_TTL_SECONDS = 30.0

# Zoom type codes. Fixed-time recurring entries carry their dates in
# ``occurrences`` on the detail endpoint; "no fixed time" recurrences (3, 6)
# have no start to block, and instant/PMI meetings are not scheduled time.
_MEETING_SCHEDULED = 2
_MEETING_RECURRING_FIXED = 8
_WEBINAR_SCHEDULED = 5
_WEBINAR_RECURRING_FIXED = 9
_DEFAULT_DURATION_MIN = 60

Interval = tuple[datetime, datetime]

# (fetched monotonic, every upcoming interval) — Zoom's list calls take no date
# window, so the cache holds everything and each caller filters its own window.
_cache: dict[str, object] = {}
# One refresh at a time per process; callers queued behind it reuse its result.
_refresh_lock = threading.Lock()
# A queued caller gives up after this long rather than pinning a worker thread.
_LOCK_WAIT_SECONDS = 20.0


def reset_busy_cache() -> None:
    _cache.clear()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _interval(start_raw: object, duration: object) -> Interval | None:
    start = _parse(start_raw)
    if start is None:
        return None
    minutes = duration if isinstance(duration, int) and duration > 0 else _DEFAULT_DURATION_MIN
    return start, start + timedelta(minutes=minutes)


def _get(url: str, headers: dict, params: dict | None = None) -> httpx.Response:
    try:
        return httpx.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ZoomApiError(f"Zoom unreachable: {exc.__class__.__name__}") from exc


def _pages(url: str, headers: dict, key: str, params: dict) -> list[dict]:
    items: list[dict] = []
    token = ""
    for _ in range(_MAX_PAGES):
        query = {**params, "page_size": "300", **({"next_page_token": token} if token else {})}
        resp = _get(url, headers, query)
        if not resp.is_success:
            raise ZoomApiError(_zoom_refusal(resp), status_code=resp.status_code)
        data = resp.json() or {}
        items.extend(i for i in data.get(key) or [] if isinstance(i, dict))
        token = data.get("next_page_token") or ""
        if not token:
            break
    return items


def _occurrences(kind: str, entry_id: object, headers: dict) -> list[Interval]:
    """Expand one recurring meeting/webinar into its dated occurrences."""
    resp = _get(f"{_ZOOM_API_BASE}/{kind}/{entry_id}", headers)
    if resp.status_code == 404:  # deleted between the list and this call
        return []
    if not resp.is_success:
        raise ZoomApiError(_zoom_refusal(resp), status_code=resp.status_code)
    found = []
    for occ in (resp.json() or {}).get("occurrences") or []:
        if isinstance(occ, dict) and occ.get("status") != "deleted":
            interval = _interval(occ.get("start_time"), occ.get("duration"))
            if interval:
                found.append(interval)
    return found


def _collect(kind: str, list_params: dict, scheduled: int, recurring: int, headers: dict) -> list[Interval]:
    user = settings.paul_booking_zoom_user
    out: list[Interval] = []
    for entry in _pages(f"{_ZOOM_API_BASE}/users/{user}/{kind}", headers, kind, list_params):
        if entry.get("type") == scheduled:
            interval = _interval(entry.get("start_time"), entry.get("duration"))
            if interval:
                out.append(interval)
        elif entry.get("type") == recurring:
            out.extend(_occurrences(kind, entry.get("id"), headers))
    return out


def get_paul_busy(window_start: datetime, window_end: datetime, *, fresh: bool = False) -> list[Interval]:
    """Paul's Zoom meetings and webinars overlapping [window_start, window_end).

    Cached in-process for 60s (``fresh=True`` skips that cache, for the check
    made just before a hold is taken, but still reuses a lookup that finished
    after the call began). Raises ``ZoomApiError`` if Zoom is not configured,
    unreachable or refuses (e.g. a scope not yet granted); a failure is
    remembered for 30s either way.
    """
    if not is_configured():
        raise ZoomApiError("Zoom is not configured")
    asked = time.monotonic()
    if not fresh:
        cached = _cache.get("entry")
        if cached and asked - cached[0] < _CACHE_TTL_SECONDS:  # type: ignore[index]
            return _within(cached[1], window_start, window_end)  # type: ignore[index]
    _raise_recent_failure(asked)

    if not _refresh_lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise ZoomApiError("Zoom lookup is taking too long")
    try:
        cached = _cache.get("entry")
        if cached and cached[0] >= asked:  # type: ignore[index]
            return _within(cached[1], window_start, window_end)  # type: ignore[index]
        _raise_recent_failure(time.monotonic())
        try:
            intervals = _fetch_all()
        except ZoomApiError as exc:
            _cache["failure"] = (time.monotonic(), str(exc))
            raise
        _cache.pop("failure", None)
        _cache["entry"] = (time.monotonic(), intervals)
    finally:
        _refresh_lock.release()
    return _within(intervals, window_start, window_end)


def _within(intervals: list[Interval], window_start: datetime, window_end: datetime) -> list[Interval]:
    return [i for i in intervals if i[0] < window_end and i[1] > window_start]


def _raise_recent_failure(now: float) -> None:
    failure = _cache.get("failure")
    if failure and now - failure[0] < _FAILURE_TTL_SECONDS:  # type: ignore[index]
        raise ZoomApiError(f"Zoom lookup failed recently: {failure[1]}")  # type: ignore[index]


def _fetch_all() -> list[Interval]:
    try:
        headers = {"Authorization": f"Bearer {_get_access_token()}"}
    except httpx.HTTPError as exc:
        raise ZoomApiError(f"Zoom authentication failed: {exc.__class__.__name__}") from exc
    intervals = _collect("meetings", {"type": "upcoming"}, _MEETING_SCHEDULED, _MEETING_RECURRING_FIXED, headers)
    intervals += _collect("webinars", {}, _WEBINAR_SCHEDULED, _WEBINAR_RECURRING_FIXED, headers)
    return intervals
