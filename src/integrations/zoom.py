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

# Per-webinar question cache.
# {webinar_id: {"grade": {"title": ..., "answers": [...]}, "school": {...}, "questions": {...}}}
# Cleared only on process restart — question config rarely changes.
_question_cache: dict[str, dict[str, dict | None]] = {}


def _resolve_questions(zoom_webinar_id: str, token: str) -> dict[str, dict | None]:
    """Fetch and cache Zoom custom question config (title + allowed answers) for a webinar."""
    if zoom_webinar_id in _question_cache:
        return _question_cache[zoom_webinar_id]

    result: dict[str, dict | None] = {"grade": None, "school": None, "questions": None}
    try:
        resp = httpx.get(
            f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/registrants/questions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        for q in resp.json().get("custom_questions", []):
            title: str = q.get("title", "")
            answers: list[str] = q.get("answers") or []
            lower = title.lower()
            entry = {"title": title, "answers": answers}
            if "grade" in lower and result["grade"] is None:
                result["grade"] = entry
            elif "school" in lower and result["school"] is None:
                result["school"] = entry
            elif "question" in lower and result["questions"] is None:
                result["questions"] = entry
        logger.debug("Zoom questions resolved — webinar=%s result=%s", zoom_webinar_id, result)
    except Exception as exc:
        logger.warning("Zoom question fetch failed — webinar=%s error=%s", zoom_webinar_id, exc)

    _question_cache[zoom_webinar_id] = result
    return result


def _match_answer(value: str, answers: list[str]) -> str | None:
    """Match value against Zoom's predefined answer list.

    Returns the exact Zoom answer string, or None if no match found.
    For free-text questions (empty answers list), returns the value as-is.
    """
    if not answers:
        return value
    # Exact match first
    for a in answers:
        if a.lower() == value.lower():
            return a
    # Contains match — e.g. our school name "Lincoln High" inside "Lincoln High School"
    for a in answers:
        if value.lower() in a.lower() or a.lower() in value.lower():
            return a
    logger.warning("Zoom answer match failed — value=%r not in answers=%s", value, answers)
    return None


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
    grade: str | None = None,
    school: str | None = None,
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
        q_config = _resolve_questions(zoom_webinar_id, token)
        custom_questions = []
        if grade and q_config["grade"]:
            # Grade may be comma-separated (multi-select); match each part individually
            parts = [g.strip() for g in grade.split(",") if g.strip()]
            matched_parts = [_match_answer(p, q_config["grade"]["answers"]) for p in parts]
            matched_grade = ",".join(m for m in matched_parts if m)
            if matched_grade:
                custom_questions.append({"title": q_config["grade"]["title"], "value": matched_grade})
        if school and q_config["school"]:
            matched = _match_answer(school, q_config["school"]["answers"])
            if matched:
                custom_questions.append({"title": q_config["school"]["title"], "value": matched})
        if questions and q_config["questions"]:
            matched = _match_answer(questions, q_config["questions"]["answers"])
            if matched:
                custom_questions.append({"title": q_config["questions"]["title"], "value": matched})
        if custom_questions:
            payload["custom_questions"] = custom_questions

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

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Zoom webinar registration failed — webinar=%s status=%s body=%s",
            zoom_webinar_id,
            exc.response.status_code,
            exc.response.text,
        )
        return None
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
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Zoom get_webinar failed — webinar=%s status=%s body=%s",
            zoom_webinar_id,
            exc.response.status_code,
            exc.response.text,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Zoom get_webinar failed — webinar=%s error=%s",
            zoom_webinar_id,
            exc,
        )
        return None
