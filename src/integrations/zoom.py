"""Zoom Server-to-Server OAuth client for webinar registration."""

from __future__ import annotations

import base64
import logging
import time

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
_ZOOM_API_BASE = "https://api.zoom.us/v2"

# In-process token cache — refreshed when within 60s of expiry
_token_cache: dict[str, object] = {"access_token": None, "expires_at": 0.0}


def _get_access_token() -> str:
    """Return a valid Bearer token, fetching a new one if needed."""
    now = time.time()
    if _token_cache["access_token"] and now < float(_token_cache["expires_at"]):
        return str(_token_cache["access_token"])

    credentials = base64.b64encode(
        f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()
    ).decode()

    resp = httpx.post(
        _ZOOM_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "account_credentials",
            "account_id": settings.zoom_account_id,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    # Subtract 60s to avoid edge-case races at expiry
    _token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    return str(_token_cache["access_token"])


def register_webinar(
    zoom_webinar_id: str,
    email: str,
    first_name: str | None,
    last_name: str | None,
    questions: str | None = None,
) -> str | None:
    """
    Register an attendee for a Zoom webinar via the Zoom API.

    Returns the Zoom ``registrant_id`` string on success, or ``None`` if
    credentials are not configured or the API call fails.  Failures are
    intentionally non-fatal — the caller's own DB record has already been
    committed before this is called.
    """
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — skipping Zoom registration")
        return None

    try:
        token = _get_access_token()

        payload: dict[str, object] = {
            "email": email,
            "first_name": first_name or "",
            "last_name": last_name or "",
        }
        if questions:
            payload["custom_questions"] = [
                {"title": "Questions for the presenter", "value": questions}
            ]

        resp = httpx.post(
            f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/registrants",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
        registrant_id: str | None = resp.json().get("id")
        logger.info(
            "Zoom registration created — webinar=%s registrant=%s",
            zoom_webinar_id,
            registrant_id,
        )
        return registrant_id

    except Exception as exc:
        logger.warning(
            "Zoom webinar registration failed — webinar=%s error=%s",
            zoom_webinar_id,
            exc,
        )
        return None


def get_webinar(zoom_webinar_id: str) -> dict | None:
    """
    Fetch webinar details from the Zoom API.

    Returns a dict containing at minimum ``join_url``, ``start_url``, and
    ``registration_url`` (when present), or ``None`` if credentials are not
    configured or the API call fails.
    """
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — skipping Zoom webinar fetch")
        return None

    try:
        token = _get_access_token()
        resp = httpx.get(
            f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info("Zoom webinar fetched — webinar=%s", zoom_webinar_id)
        return resp.json()
    except Exception as exc:
        logger.warning(
            "Zoom get_webinar failed — webinar=%s error=%s",
            zoom_webinar_id,
            exc,
        )
        return None
