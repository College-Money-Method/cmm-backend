"""Template preview + send-test endpoints (super_admin ONLY).

Stateless: renders arbitrary template content (category + subject + body_json,
straight from the compose editor — no saved row required) against a chosen
(school, webinar, contact) context using the SAME render path as real sends, so
what the admin previews is exactly what recipients get. Also serves the webinar
picker (webinars mapped to a school) the preview dialog needs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth.deps import AdminDep
from src.config import settings
from src.db.deps import DbDep
from src.emails.broadcast_schemas import SendTestResultOut
from src.emails.link_resolver import resolve_plain_text
from src.emails.renderer import render_email
from src.emails.ses_client import send_email
from src.emails.template_preview import build_preview_context
from src.schools.models import Contact
from src.workshops.models import PortalMapping, Webinar

router = APIRouter(prefix="/api/v1/emails/preview", tags=["emails"])

TemplateCategory = Literal["broadcast", "workshop_automation"]

# Maps a template category to the EmailSendLog.source its test send logs under
# (must satisfy the ck_email_send_log_source check constraint). Test sends carry
# no broadcast_id/automation_id, so they never affect any campaign's analytics.
_SOURCE_BY_CATEGORY = {"broadcast": "broadcast", "workshop_automation": "pre_workshop"}


class PreviewContextIn(BaseModel):
    category: TemplateCategory
    subject: str = Field(min_length=1)
    body_json: dict
    school_id: uuid.UUID
    # Required for workshop_automation (fills date/time/workshop tags); ignored
    # for broadcast.
    webinar_id: uuid.UUID | None = None
    # Optional; only affects counselor_name when the contact IS the hub_admin.
    contact_id: uuid.UUID | None = None


class TemplatePreviewOut(BaseModel):
    subject: str  # merge-tags resolved
    html: str


class PreviewWebinarOut(BaseModel):
    webinar_id: uuid.UUID
    workshop_name: str
    start_datetime: datetime | None
    cycle_name: str | None


def _render(db: Session, payload: PreviewContextIn) -> tuple[str, str, str]:
    """Resolve context and render -> (html, text, resolved_subject)."""
    ctx = build_preview_context(
        db,
        category=payload.category,
        school_id=payload.school_id,
        webinar_id=payload.webinar_id,
        contact_id=payload.contact_id,
    )
    html, text = render_email(
        payload.body_json,
        ctx.replacements,
        payload.subject,
        school_slug=ctx.school_slug,
        origin=settings.app_public_url or None,
    )
    subject = resolve_plain_text(payload.subject, ctx.replacements)
    return html, text, subject


@router.get("/webinars", response_model=list[PreviewWebinarOut])
def list_school_webinars(
    _admin: AdminDep, db: DbDep, school_id: uuid.UUID = Query(...)
) -> list[PreviewWebinarOut]:
    """Webinars mapped to a school (via PortalMapping), newest first — feeds the
    workshop-template preview's webinar picker."""
    webinars = db.scalars(
        select(Webinar)
        .join(PortalMapping, PortalMapping.webinar_id == Webinar.id)
        .where(PortalMapping.school_id == school_id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cycle))
        .order_by(Webinar.start_datetime.desc())
    ).all()
    return [
        PreviewWebinarOut(
            webinar_id=w.id,
            workshop_name=w.workshop.name if w.workshop else "(unknown workshop)",
            start_datetime=w.start_datetime,
            cycle_name=w.cycle.name if w.cycle else None,
        )
        for w in webinars
    ]


@router.post("/render", response_model=TemplatePreviewOut)
def render_preview(payload: PreviewContextIn, _admin: AdminDep, db: DbDep) -> TemplatePreviewOut:
    html, _text, subject = _render(db, payload)
    return TemplatePreviewOut(subject=subject, html=html)


@router.post("/send-test", response_model=SendTestResultOut)
def send_preview_test(payload: PreviewContextIn, admin: AdminDep, db: DbDep) -> SendTestResultOut:
    """Send the rendered preview to the requesting admin only. Recipient is the
    admin's own Contact email when they have one, else their authenticated login
    email (super_admins are not Contacts)."""
    contact = db.scalar(select(Contact).where(Contact.user_id == admin.user_id))
    to = contact.email if contact and contact.email else admin.email
    if not to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No email address on file for the current admin — cannot send a test",
        )
    html, text, subject = _render(db, payload)
    send_email(
        db,
        to=to,
        subject=subject,
        html=html,
        text=text,
        source=_SOURCE_BY_CATEGORY[payload.category],
    )
    return SendTestResultOut(sent_to=to, used_sample_contact=payload.contact_id is not None)
