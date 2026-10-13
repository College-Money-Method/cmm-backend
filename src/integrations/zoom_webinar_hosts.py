"""Zoom co-host lookup for a webinar: alternative hosts plus panelists.

Kept out of ``zoom.py`` (already large); reuses its auth and base URL.
Needs the S2S app scopes to read webinars and list panelists.
"""

from __future__ import annotations

import logging
import re

import httpx

from src.integrations.zoom import _ZOOM_API_BASE, _get_access_token, _zoom_refusal, is_configured

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_MAX_PAGES = 20  # panelist lists are tiny; this only bounds a misbehaving cursor


class ZoomWebinarNotFound(Exception):
    """Zoom answered 404: the webinar was deleted, so callers should skip it."""


def _split_alternative_hosts(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    return [part for part in re.split(r"[;,]", raw) if part.strip()]


def get_webinar_cohost_emails(zoom_webinar_id: str | None) -> list[str] | None:
    """Lowercased, deduped co-host emails; ``[]`` = none, ``None`` = lookup failed.

    Raises ``ZoomWebinarNotFound`` when Zoom answers 404 (webinar deleted), so the
    caller can skip it instead of treating it as a transient failure.
    """
    if not zoom_webinar_id or not is_configured():
        return None
    try:
        headers = {"Authorization": f"Bearer {_get_access_token()}"}
        emails: list[str] = []

        resp = httpx.get(f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}", headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 404:
            logger.warning("zoom_webinar_missing webinar=%s", zoom_webinar_id)
            raise ZoomWebinarNotFound(zoom_webinar_id)
        if not resp.is_success:
            logger.warning("Zoom webinar lookup failed (%s): %s", resp.status_code, _zoom_refusal(resp))
            return None
        settings_block = (resp.json() or {}).get("settings") or {}
        emails.extend(_split_alternative_hosts(settings_block.get("alternative_hosts")))

        next_page_token = ""
        for _ in range(_MAX_PAGES):
            params = {"page_size": "300"}
            if next_page_token:
                params["next_page_token"] = next_page_token
            resp = httpx.get(
                f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/panelists",
                headers=headers,
                params=params,
                timeout=_TIMEOUT,
            )
            if resp.status_code == 404:
                logger.warning("zoom_webinar_missing webinar=%s", zoom_webinar_id)
                raise ZoomWebinarNotFound(zoom_webinar_id)
            if not resp.is_success:
                logger.warning("Zoom panelists lookup failed (%s): %s", resp.status_code, _zoom_refusal(resp))
                return None
            data = resp.json() or {}
            for panelist in data.get("panelists") or []:
                if isinstance(panelist, dict) and isinstance(panelist.get("email"), str):
                    emails.append(panelist["email"])
            next_page_token = data.get("next_page_token") or ""
            if not next_page_token:
                break
    except ZoomWebinarNotFound:
        raise
    except Exception:  # noqa: BLE001 - any Zoom/network/parse failure means "unknown", never "no co-hosts"
        logger.warning("Zoom co-host lookup failed for webinar %s", zoom_webinar_id, exc_info=True)
        return None

    result = list(dict.fromkeys(e.strip().lower() for e in emails if e.strip()))
    if not result:
        logger.warning("Zoom returned no co-hosts for webinar %s", zoom_webinar_id)
    return result
