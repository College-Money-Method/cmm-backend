"""Co-host questions page API: a public tokenized read, plus admin link management.

The public route is reachable by anyone holding the link, so every way a token
can fail (malformed, unknown, rotated away) answers with the
same 404 — it must not reveal whether a token ever existed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.schools.display_timezone import resolve_display_timezone
from src.workshops.host_access import (
    approved_registrations,
    effective_end,
    ensure_host_access_token,
    host_questions_url,
    rotate_host_access_token,
)
from src.workshops.host_questions_schemas import (
    HostLinkOut,
    HostQuestionOut,
    HostQuestionsOut,
    HostWebinarOut,
)
from src.workshops.models import Webinar, WorkshopRegistration

router = APIRouter(prefix="/api/v1/workshops", tags=["workshop-hosts"])

# secrets.token_urlsafe(32) is always exactly 43 characters of this alphabet.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_INVALID_LINK = "This link is invalid or has been replaced."
# Sent on every public response, 404s included (HTTPException drops headers set on Response).
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _as_aware(value: datetime) -> datetime:
    # SQLite (tests) hands timestamps back naive; Postgres returns aware UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _host_questions_out(regs: list[WorkshopRegistration]) -> list[HostQuestionOut]:
    """Only registrants who asked something, newest first, stripped of identity."""
    asked = [
        HostQuestionOut(
            question=reg.questions.strip(),
            grade=reg.grade,
            # Zoom's registration time when we have it, else when the row arrived.
            submitted_at=_as_aware(reg.registration_time or reg.created_at),
        )
        for reg in regs
        if reg.questions and reg.questions.strip()
    ]
    return sorted(asked, key=lambda q: q.submitted_at, reverse=True)


@router.get("/public/webinar-hosts/{token}", response_model=HostQuestionsOut)
def get_host_questions(token: str, response: Response, db: DbDep):
    response.headers.update(_PRIVATE_HEADERS)
    if not _TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail=_INVALID_LINK, headers=_PRIVATE_HEADERS)

    webinar = db.execute(
        select(Webinar).where(Webinar.host_access_token == token).options(selectinload(Webinar.workshop))
    ).scalar_one_or_none()
    if webinar is None:
        raise HTTPException(status_code=404, detail=_INVALID_LINK, headers=_PRIVATE_HEADERS)
    end = effective_end(webinar)

    return HostQuestionsOut(
        webinar=HostWebinarOut(
            workshop_title=webinar.workshop.name if webinar.workshop else None,
            start_datetime=webinar.start_datetime,
            end_datetime=webinar.end_datetime,
            display_timezone=resolve_display_timezone().key,
        ),
        ended=end is not None and datetime.now(timezone.utc) > _as_aware(end),
        questions=_host_questions_out(approved_registrations(db, webinar.id)),
    )


def _host_link_out(webinar: Webinar, token: str) -> HostLinkOut:
    try:
        url = host_questions_url(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Public site URL is not configured") from exc
    return HostLinkOut(url=url)


def _get_webinar_or_404(db, webinar_id: uuid.UUID) -> Webinar:
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")
    return webinar


@router.post("/webinars/{webinar_id}/host-link", response_model=HostLinkOut)
def get_host_link(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    webinar = _get_webinar_or_404(db, webinar_id)
    token = ensure_host_access_token(db, webinar)
    return _host_link_out(webinar, token)


@router.post("/webinars/{webinar_id}/host-link/rotate", response_model=HostLinkOut)
def rotate_host_link(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    webinar = _get_webinar_or_404(db, webinar_id)
    token = rotate_host_access_token(db, webinar)
    return _host_link_out(webinar, token)
