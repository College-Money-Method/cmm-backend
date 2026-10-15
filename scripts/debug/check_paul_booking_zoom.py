"""Read-only check that Zoom is ready for "Request a call with Paul".

Run from the repo root after Paul grants the scopes:

    uv run python scripts/debug/check_paul_booking_zoom.py

Uses the Zoom app credentials from this environment's settings. Prints the
missing scopes (never a token), then runs the real busy lookup over the booking
window. Creates nothing in Zoom.
"""

from __future__ import annotations

import base64
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from src.config import settings
from src.integrations.zoom import ZoomApiError, forget_access_token, is_configured
from src.integrations.zoom_paul_calendar import get_paul_busy
from src.paul_booking.rules import HORIZON, MIN_NOTICE

# What each Zoom call in the booking flow needs. Zoom accepts the plain or the
# ":admin" variant of a scope.
REQUIRED = {
    "meeting:read:list_meetings": "list Paul's meetings (slots)",
    "webinar:read:list_webinars": "list Paul's webinars (slots)",
    "meeting:read:meeting": "expand recurring meetings (slots)",
    "webinar:read:webinar": "expand recurring webinars (slots)",
    "meeting:write:meeting": "create the meeting when Paul approves",
}


def granted_scopes() -> set[str]:
    credentials = base64.b64encode(f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()).decode()
    resp = httpx.post(
        "https://zoom.us/oauth/token",
        headers={"Authorization": f"Basic {credentials}"},
        data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
        timeout=15.0,
    )
    resp.raise_for_status()
    return set((resp.json().get("scope") or "").split())


def main() -> int:
    if not is_configured():
        print("FAIL Zoom credentials are not set in this environment")
        return 1
    granted = granted_scopes()
    missing = [s for s in REQUIRED if s not in granted and f"{s}:admin" not in granted]
    for scope, purpose in REQUIRED.items():
        print(f"{'ok  ' if scope not in missing else 'MISSING'} {scope}:admin  ({purpose})")

    now = datetime.now(UTC)
    forget_access_token()  # make the lookup use a token with today's scopes
    try:
        busy = get_paul_busy(now + MIN_NOTICE, now + HORIZON, fresh=True)
    except ZoomApiError as exc:
        print(f"FAIL busy lookup for {settings.paul_booking_zoom_user}: {exc}")
        return 1
    print(f"ok   busy lookup for {settings.paul_booking_zoom_user}: {len(busy)} busy interval(s) in the booking window")
    if missing:
        print("FAIL approving a request will fail until the missing scopes are granted")
        return 1
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
