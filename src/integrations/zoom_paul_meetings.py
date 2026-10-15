"""Create the Zoom meeting for an approved call with Paul."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from src.config import settings
from src.integrations.zoom import (
    _ZOOM_API_BASE,
    ZoomApiError,
    _get_access_token,
    _zoom_error_code,
    _zoom_refusal,
    is_configured,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
# Zoom's documented caps on these two fields.
_TOPIC_MAX = 200
_AGENDA_MAX = 2000


def create_paul_meeting(
    *, topic: str, agenda: str | None, start_at: datetime, duration_minutes: int, timezone_name: str
) -> tuple[str, str]:
    """Schedule the meeting on Paul's account; returns ``(meeting_id, join_url)``.

    A waiting room is on and the guest cannot join before Paul, since the guest
    is an outside visitor. Raises ``ZoomApiError`` on any refusal or network
    failure, with Zoom's own words in the message.
    """
    if not is_configured():
        raise ZoomApiError("Zoom is not configured")
    start_utc = start_at.astimezone(timezone.utc) if start_at.tzinfo else start_at.replace(tzinfo=timezone.utc)
    body = {
        "topic": topic[:_TOPIC_MAX],
        "type": 2,
        "start_time": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": duration_minutes,
        "timezone": timezone_name,
        "settings": {"waiting_room": True, "join_before_host": False},
    }
    if agenda:
        body["agenda"] = agenda[:_AGENDA_MAX]
    try:
        resp = httpx.post(
            f"{_ZOOM_API_BASE}/users/{settings.paul_booking_zoom_user}/meetings",
            headers={"Authorization": f"Bearer {_get_access_token()}"},
            json=body,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ZoomApiError(f"Zoom unreachable: {exc.__class__.__name__}") from exc
    if not resp.is_success:
        raise ZoomApiError(_zoom_refusal(resp), status_code=resp.status_code, code=_zoom_error_code(resp))
    data = resp.json() or {}
    meeting_id, join_url = data.get("id"), data.get("join_url")
    if not meeting_id or not join_url:
        raise ZoomApiError("Zoom created a meeting but returned no id or join link")
    return str(meeting_id), str(join_url)
