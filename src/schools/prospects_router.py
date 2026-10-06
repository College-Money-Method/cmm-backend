"""Public fit-check intake: duplicate check, preview creation, admin recent list.

Registered before the main schools router so the literal ``/prospects`` paths
are not parsed as a ``/{school_id}``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from src.auth.deps import AdminDep
from src.auth.rate_limit import allow, client_ip
from src.db.deps import DbDep
from src.guest_contacts.models import GuestContact
from src.guest_contacts.parent_detection import detect_parent
from src.guest_contacts.spam_detection import detect_spam
from src.schools.models import School
from src.schools.prospect_logo import read_logo, store_logo
from src.schools.prospect_matching import (
    ProspectMatch,
    find_prospect_match,
    initials_password,
)
from src.schools.prospect_schemas import (
    ProspectCheckResponse,
    ProspectCreateResponse,
    ProspectForm,
    RecentProspect,
)
from src.schools.slug_utils import RESERVED_SLUGS, unique_slug_db
from src.schools.src_session import issue_token
from src.storage.s3_client import S3ClientDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/schools/prospects", tags=["school-prospects"])

PROSPECT_SOURCE = "landing_fit_check"
CONTACT_SOURCE = "school_fit_check"
PREVIEW_DAYS = 30
_CHECK_LIMIT, _SUBMIT_LIMIT, _WINDOW = 20, 3, 600.0
_MAX_QUIZ_BYTES = 10_000


def _rate_limit(request: Request, bucket: str, limit: int) -> None:
    if not allow(f"{bucket}:{client_ip(request)}", limit=limit, window_seconds=_WINDOW):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests. Please wait a few minutes and try again.")


@router.get("/check", response_model=ProspectCheckResponse)
def check_prospect(
    request: Request,
    db: DbDep,
    name: str = Query(min_length=1, max_length=200),
    state: str = Query(default="", max_length=2),
) -> ProspectCheckResponse:
    """Tell the fit-check form whether a school is already known, before submit."""
    _rate_limit(request, "prospect-check", _CHECK_LIMIT)
    match = find_prospect_match(db, name, state.strip() or None)
    return ProspectCheckResponse(status=match.status, slug=match.slug, expires_at=match.expires_at)


def _parse_form(**fields) -> ProspectForm:
    quiz_raw = fields.pop("quiz_answers", None)
    if quiz_raw:
        if len(quiz_raw) > _MAX_QUIZ_BYTES:
            raise HTTPException(status_code=422, detail="quiz_answers is too large")
        try:
            quiz = json.loads(quiz_raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="quiz_answers must be valid JSON") from exc
        if not isinstance(quiz, dict):
            raise HTTPException(status_code=422, detail="quiz_answers must be a JSON object")
        fields["quiz_answers"] = quiz
    try:
        return ProspectForm(**fields)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False))) from exc


def _contact_message(form: ProspectForm) -> str:
    lines = ["School fit check", f"School: {form.school_name} ({form.city or '-'}, {form.state})"]
    if form.role:
        lines.append(f"Role: {form.role}")
    for key, value in (form.quiz_answers or {}).items():
        shown = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        lines.append(f"- {key}: {shown}")
    return "\n".join(lines)[:5000]


def _save_contact(db, form: ProspectForm, *, school_id, spam_reason: str | None) -> None:
    message = _contact_message(form)
    parent_reason = detect_parent(role=form.role, message=message)
    db.add(GuestContact(
        first_name=form.first_name, last_name=form.last_name, email=str(form.email),
        role=form.role, school_name=form.school_name, message=message,
        school_id=school_id, source=CONTACT_SOURCE, quiz_answers=form.quiz_answers,
        is_spam=spam_reason is not None, spam_reason=spam_reason,
        is_parent=parent_reason is not None, parent_reason=parent_reason,
    ))


def _open_preview(db, s3, form: ProspectForm, match: ProspectMatch, logo) -> tuple[School, str | None]:
    """Create the school, or switch preview on for an unactivated pipeline row.

    Returns the school and the password to show (None when an existing password
    was kept, since that one was not generated here).
    """
    expires = datetime.now(timezone.utc) + timedelta(days=PREVIEW_DAYS)
    school = match.school
    if school is None:
        slug = unique_slug_db(form.school_name, db)
        if slug in RESERVED_SLUGS:
            slug = unique_slug_db(f"{form.school_name} school", db)
        school = School(
            name=form.school_name, slug=slug, city=form.city, state=form.state,
            is_current_customer=False, is_cmm_website_activated=True,
            is_src_preview=True, prospect_source=PROSPECT_SOURCE, src_preview_expires_at=expires,
            cmm_website_password=initials_password(form.school_name),
        )
        db.add(school)
        db.flush()
        shown_password: str | None = school.cmm_website_password
    else:
        school.is_cmm_website_activated = True
        school.is_src_preview = True
        school.prospect_source = school.prospect_source or PROSPECT_SOURCE
        school.src_preview_expires_at = expires
        shown_password = None
        if not school.cmm_website_password:
            school.cmm_website_password = shown_password = initials_password(school.name)
    if logo is not None and not school.logo_url:
        stored = store_logo(s3, school.id, logo)
        if stored:
            school.logo_url, school.logo_thumb_url = stored
    return school, shown_password


@router.post("", response_model=ProspectCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_prospect(
    request: Request,
    db: DbDep,
    s3: S3ClientDep,
    school_name: str = Form(...),
    state: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    city: str | None = Form(default=None),
    role: str | None = Form(default=None),
    quiz_answers: str | None = Form(default=None),
    website: str | None = Form(default=None),
    logo: UploadFile | None = File(default=None),
) -> ProspectCreateResponse:
    """Open a limited preview of the resource center for a school (public)."""
    _rate_limit(request, "prospect-submit", _SUBMIT_LIMIT)
    form = _parse_form(school_name=school_name, city=city, state=state, first_name=first_name,
                       last_name=last_name, email=email, role=role, quiz_answers=quiz_answers,
                       website=website)

    spam_reason = detect_spam(
        first_name=form.first_name, last_name=form.last_name, email=str(form.email),
        school_name=form.school_name, message=_contact_message(form), honeypot=form.website,
    )
    if spam_reason:
        # Quarantine and answer like an existing partner so a bot learns nothing
        _save_contact(db, form, school_id=None, spam_reason=spam_reason)
        db.commit()
        return ProspectCreateResponse(status="existing_partner")

    logo_data = await read_logo(logo)

    for attempt in (1, 2):
        match = find_prospect_match(db, form.school_name, form.state)
        try:
            if match.status == "available":
                school, password = _open_preview(db, s3, form, match, logo_data)
                token = issue_token(school)[0]
                _save_contact(db, form, school_id=school.id, spam_reason=None)
                db.commit()
                return ProspectCreateResponse(
                    status="preview_ready", school_id=school.id, slug=school.slug,
                    school_name=school.name, password=password,
                    expires_at=school.src_preview_expires_at, session_token=token,
                )
            school = match.school
            _save_contact(db, form, school_id=school.id, spam_reason=None)
            db.commit()
            return ProspectCreateResponse(
                status=match.status, school_id=school.id, slug=school.slug,
                school_name=school.name, expires_at=match.expires_at,
            )
        except IntegrityError:
            # Lost a race on the unique slug; the winner's row now matches
            db.rollback()
            if attempt == 2:
                logger.exception("Prospect creation failed twice for %s", form.school_name)
                raise HTTPException(status_code=503, detail="Please try again in a moment.")
    raise HTTPException(status_code=503, detail="Please try again in a moment.")  # pragma: no cover


@router.get("/recent", response_model=list[RecentProspect])
def recent_prospects(
    _admin: AdminDep, db: DbDep, limit: int = Query(default=10, ge=1, le=50)
) -> list[RecentProspect]:
    """Admin: newest self-serve prospects with their contact details."""
    schools = (
        db.query(School)
        .filter(School.prospect_source == PROSPECT_SOURCE)
        .order_by(School.created_at.desc())
        .limit(limit)
        .all()
    )
    out: list[RecentProspect] = []
    for s in schools:
        contact = (
            db.query(GuestContact)
            .filter(GuestContact.school_id == s.id, GuestContact.source == CONTACT_SOURCE,
                    GuestContact.is_spam.is_(False))
            .order_by(GuestContact.created_at.desc())
            .first()
        )
        name = " ".join(p for p in (contact.first_name, contact.last_name) if p) if contact else None
        out.append(RecentProspect(
            school_id=s.id, school_name=s.name, slug=s.slug, logo_thumb_url=s.logo_thumb_url,
            city=s.city, state=s.state, contact_name=name or None,
            email=contact.email if contact else None, role=contact.role if contact else None,
            created_at=s.created_at, src_preview_expires_at=s.src_preview_expires_at,
            is_src_preview=s.is_src_preview,
        ))
    return out
