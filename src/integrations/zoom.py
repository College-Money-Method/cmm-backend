"""Zoom Server-to-Server OAuth client for webinar registration."""

from __future__ import annotations

import base64
import logging
import time

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class ZoomApiError(RuntimeError):
    """Zoom answered, and the answer was a refusal.

    Exists so a caller can repeat what Zoom said instead of guessing. The
    guesses are what made this necessary: a missing API scope, an expired
    credential and a genuinely deleted recording all arrived as the same empty
    result, and the operator-facing message picked one of them at random.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _zoom_refusal(resp: httpx.Response) -> str:
    """Zoom's own words for why a call failed, safe to show an operator.

    Zoom answers errors with ``{"code": ..., "message": ...}``, and the message
    names the actual problem — which scope is missing, that the meeting does not
    exist. Nothing on this path carries a credential: the token travels in the
    request header and is never echoed back. Truncated anyway, because an
    unexpected body should not become an unbounded job-row message.
    """
    try:
        body = resp.json()
        detail = str(body.get("message") or "").strip()
        code = body.get("code")
    except Exception:
        detail, code = "", None
    if not detail:
        detail = resp.text[:300].strip() or "no detail"
    return f"HTTP {resp.status_code}" + (f" (code {code})" if code else "") + f": {detail}"


# Zoom rejects a custom-question answer longer than this with
# {"code":300,"message":"Invalid parameter: custom_questions."}, which used to
# strand the registration entirely — the parent's row committed, but Zoom never
# issued a join link. Their longest accepted answer in production was 124 chars
# and the shortest rejected one 142, so the ceiling sits at 128.
_ZOOM_ANSWER_MAX_CHARS = 128

# Zoom's batch registration endpoint takes at most this many people per call.
_ZOOM_BATCH_MAX = 30

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

    For free-text questions (empty answers list), returns the value capped at
    Zoom's answer length limit. Only the copy Zoom keeps is shortened — our own
    ``workshop_registrations.questions`` still holds what the parent wrote, and
    that is the copy the workshop host reads.
    """
    if not answers:
        return value[:_ZOOM_ANSWER_MAX_CHARS]
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


def batch_register_webinar(
    zoom_webinar_id: str,
    people: list[dict[str, str | None]],
) -> dict[str, str]:
    """Register a group of attendees in one call, returning email -> registrant_id.

    Zoom's batch endpoint accepts only name and email — it has no
    ``custom_questions`` field, so it cannot be rejected over a stale dropdown
    answer list or an over-long free-text answer, the two faults that stranded
    registrations in the first place. Everything the host actually reads (grade,
    school, the parent's question) already lives in our own tables, so nothing
    is lost by leaving it out of Zoom's copy.

    Confirmation emails are on: the join link Zoom mails back is the entire
    point of re-sending these.

    Raises ``ZoomApiError`` when Zoom refuses, rather than returning an empty
    result — a backfill that quietly registers nobody is the failure this is
    meant to repair.
    """
    if not people:
        return {}
    if len(people) > _ZOOM_BATCH_MAX:
        raise ValueError(f"batch takes at most {_ZOOM_BATCH_MAX} registrants, got {len(people)}")

    token = _get_access_token()
    resp = httpx.post(
        f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/batch_registrants",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "auto_approve": True,
            "registrants_confirmation_email": True,
            "registrants": [
                {
                    "email": p["email"],
                    "first_name": p.get("first_name") or "",
                    "last_name": p.get("last_name") or "",
                }
                for p in people
            ],
        },
        timeout=30.0,
    )
    if resp.is_error:
        raise ZoomApiError(_zoom_refusal(resp), resp.status_code)

    created = {
        str(r["email"]): str(r["registrant_id"])
        for r in resp.json().get("registrants", [])
        if r.get("email") and r.get("registrant_id")
    }
    logger.info(
        "Zoom batch registration — webinar=%s sent=%d created=%d",
        zoom_webinar_id,
        len(people),
        len(created),
    )
    return created


def get_webinar_participants(zoom_webinar_id: str) -> list[dict] | None:
    """
    Fetch the post-webinar participant report from the Zoom Reports API.

    Returns a flat list of participant dicts (each has ``user_email``,
    ``registrant_id``, ``join_time``, ``leave_time``, ``duration``),
    or ``None`` if credentials are not configured or the report is not yet
    available (Zoom delays report availability 5–30 min after the webinar ends).

    Requires the ``report:read:admin`` scope on the S2S OAuth app.
    Handles pagination automatically via ``next_page_token``.
    """
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — skipping participant fetch")
        return None

    try:
        token = _get_access_token()
        participants: list[dict] = []
        next_page_token = ""

        while True:
            params: dict[str, str] = {"page_size": "300"}
            if next_page_token:
                params["next_page_token"] = next_page_token

            resp = httpx.get(
                f"{_ZOOM_API_BASE}/report/webinars/{zoom_webinar_id}/participants",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            participants.extend(data.get("participants", []))

            next_page_token = data.get("next_page_token", "")
            if not next_page_token:
                break

        logger.info(
            "Zoom participant report fetched — webinar=%s count=%d",
            zoom_webinar_id,
            len(participants),
        )
        return participants

    except httpx.HTTPStatusError as exc:
        # 404 means report not yet generated; caller should retry later
        logger.warning(
            "Zoom participant report unavailable — webinar=%s status=%s body=%s",
            zoom_webinar_id,
            exc.response.status_code,
            exc.response.text,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Zoom participant report fetch failed — webinar=%s error=%s",
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


def encode_recording_uuid(recording_uuid: str) -> str:
    """URL-encode a recording UUID for use as a Zoom API path segment.

    Zoom recording UUIDs are base64 and can legitimately begin with ``/`` or
    contain ``//``. Dropped into a path unescaped, those collapse the route and
    the API answers 404 for a recording that plainly exists. Zoom's own
    documentation is explicit that such a UUID must be **double** encoded, so
    ``/`` survives one round of decoding by the gateway and still reaches the
    handler as data.

    Encoding twice unconditionally is safe: a UUID with no reserved characters
    is unchanged by either pass.
    """
    from urllib.parse import quote

    return quote(quote(recording_uuid, safe=""), safe="")


def list_account_recordings(from_date: str, to_date: str) -> list[dict] | None:
    """List cloud recordings across the whole account for a date range.

    ``GET /accounts/me/recordings`` rather than the per-host endpoint: the
    reconcile sweep has to see recordings whoever hosted them, and enumerating
    hosts first would make a missed webhook depend on the host list being
    current. Requires the ``recording:read:admin`` scope.

    ``from_date`` / ``to_date`` are ``YYYY-MM-DD``. Zoom caps the span at one
    month, which is far wider than the sweep's 48-hour window.

    Returns the recording entries (each with ``uuid``, ``id``, ``topic``,
    ``recording_files``), or ``None`` if credentials are missing or the call
    failed — ``None`` and ``[]`` mean different things to the caller, which must
    not create jobs on the strength of a failed listing.
    """
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — skipping recording list")
        return None

    try:
        token = _get_access_token()
        recordings: list[dict] = []
        next_page_token = ""

        while True:
            params: dict[str, str] = {
                "from": from_date,
                "to": to_date,
                "page_size": "300",
            }
            if next_page_token:
                params["next_page_token"] = next_page_token

            resp = httpx.get(
                f"{_ZOOM_API_BASE}/accounts/me/recordings",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            recordings.extend(data.get("meetings", []))

            next_page_token = data.get("next_page_token", "")
            if not next_page_token:
                break

        logger.info(
            "Zoom account recordings listed — from=%s to=%s count=%d",
            from_date,
            to_date,
            len(recordings),
        )
        return recordings

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Zoom recording list failed — status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        return None
    except Exception as exc:
        logger.warning("Zoom recording list failed — error=%s", exc)
        return None


def get_recording(recording_uuid: str) -> dict | None:
    """Fetch one cloud recording's metadata, including fresh download URLs.

    Called at the start of every processing run rather than reading a URL saved
    at webhook time. Zoom's ``download_url`` is only usable with a credential,
    and re-deriving it from S2S OAuth here means no Zoom credential is ever
    persisted — the webhook's ``download_token`` is deliberately dropped.

    Returns the recording object (``recording_files``, ``topic``, ``duration``,
    ...). ``None`` means one thing only — no Zoom credentials are configured.
    Every other failure raises ``ZoomApiError`` carrying Zoom's own words, so
    the job row that reports it does not have to guess between a deleted
    recording, an expired credential and a scope the app was never granted.
    """
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — cannot fetch recording")
        return None

    try:
        token = _get_access_token()
        resp = httpx.get(
            f"{_ZOOM_API_BASE}/meetings/{encode_recording_uuid(recording_uuid)}/recordings",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        refusal = _zoom_refusal(exc.response)
        logger.warning("Zoom get_recording failed — recording=%s %s", recording_uuid, refusal)
        raise ZoomApiError(refusal, exc.response.status_code) from exc
    except Exception as exc:
        logger.warning("Zoom get_recording failed — recording=%s error=%s", recording_uuid, exc)
        raise ZoomApiError(f"{type(exc).__name__}: {exc}") from exc


def recording_access_token() -> str:
    """Bearer token for streaming a ``download_url``.

    Zoom's download endpoints accept the same S2S access token as the API, so
    this is just a named accessor — it exists so callers do not reach into the
    private token helper, and so the token stays a value passed to one request
    rather than something written down anywhere.
    """
    return _get_access_token()


def delete_recording(recording_uuid: str) -> bool:
    """Delete every recording file for one meeting instance. Never raises.

    ``recording:write:admin`` is account-wide, so the call is addressed by the
    exact UUID carried on the job row and by nothing else — no topic match, no
    date range, nothing that could widen to a recording this job never touched.

    Returns True when Zoom confirmed the delete. A False is deliberately
    non-fatal for the caller: the recording is already published to Vimeo by
    this point, and Zoom's 7-day auto-delete is the backstop.
    """
    if not recording_uuid:
        logger.warning("Zoom delete_recording called with no UUID — refusing")
        return False
    if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
        logger.debug("Zoom credentials not configured — skipping recording delete")
        return False

    try:
        token = _get_access_token()
        resp = httpx.delete(
            f"{_ZOOM_API_BASE}/meetings/{encode_recording_uuid(recording_uuid)}/recordings",
            headers={"Authorization": f"Bearer {token}"},
            # Trash rather than permanent delete: it frees the cloud pool the
            # same way but leaves a 30-day undo in the Zoom UI, which costs
            # nothing and covers the case where the S3 archive also went wrong.
            params={"action": "trash"},
            timeout=30.0,
        )
        resp.raise_for_status()
        logger.info("Zoom recording deleted — recording=%s", recording_uuid)
        return True
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Zoom delete_recording failed — recording=%s status=%s body=%s",
            recording_uuid,
            exc.response.status_code,
            exc.response.text[:400],
        )
        return False
    except Exception as exc:
        logger.warning("Zoom delete_recording failed — recording=%s error=%s", recording_uuid, exc)
        return False
