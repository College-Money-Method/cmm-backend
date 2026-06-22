"""Communication template and calendar schedule endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.auth.deps import AdminDep, CounselorDep
from src.auth.schemas import CurrentUser
from src.db.deps import DbDep
from src.communications.models import CommunicationTemplate
from src.communications.schedule_model import CommunicationScheduleItem
from src.communications.template_default_date_model import CommunicationTemplateDefaultDate
from src.communications.schemas import (
    CalendarResponse,
    CommunicationTemplateCreate,
    CommunicationTemplateOut,
    CommunicationTemplateUpdate,
    ScheduleItemCreate,
    ScheduleItemOut,
    ScheduleItemUpdate,
    TemplateDefaultDateOut,
    TemplateDefaultDateUpsert,
    WebinarCalendarItem,
)
from src.workshops.models import PortalMapping, Webinar

router = APIRouter(prefix="/api/v1/communications", tags=["communications"])


def _validate_format(fmt: str, content: str | None, google_docs_url: str | None) -> None:
    """Raise 422 if required field for the given format is missing."""
    if fmt == "google_docs" and not google_docs_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="google_docs_url is required when format is 'google_docs'",
        )
    if fmt == "rich_text" and not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="content is required when format is 'rich_text'",
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────
# NOTE: All static paths (/schedule, /schedule/{id}) must be declared
# BEFORE parameterized paths (/{template_id}) — Starlette matches in order.


@router.get("/", response_model=list[CommunicationTemplateOut])
def list_templates(db: DbDep, _user: CounselorDep) -> list[CommunicationTemplate]:
    """List all active templates ordered by sort_order asc, created_at asc."""
    rows = (
        db.execute(
            select(CommunicationTemplate)
            .where(CommunicationTemplate.is_active.is_(True))
            .order_by(CommunicationTemplate.sort_order.asc(), CommunicationTemplate.created_at.asc())
        )
        .scalars()
        .all()
    )
    return list(rows)


# ── Calendar schedule endpoints (defined before /{template_id}) ───────────────


def _serialize_schedule_item(item: CommunicationScheduleItem) -> dict:
    return {
        "id": item.id,
        "school_id": item.school_id,
        "cycle_id": item.cycle_id,
        "event_type": item.event_type,
        "webinar_id": item.webinar_id,
        "template_id": item.template_id,
        "template_name": item.template.name if item.template else None,
        "scheduled_at": item.scheduled_at,
        "is_auto_generated": item.is_auto_generated,
        "notes": item.notes,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _require_school(user: CurrentUser) -> uuid.UUID:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school associated with this account")
    return user.school_id


@router.get("/schedule", response_model=CalendarResponse)
def get_schedule(
    cycle_id: Annotated[uuid.UUID, Query()],
    db: DbDep,
    user: CounselorDep,
    school_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict:
    """Return webinars and schedule items for a cycle. Admins can pass school_id;
    counselors always use their own school from the JWT."""
    if user.role == "super_admin" and school_id:
        effective_school_id = school_id
    else:
        effective_school_id = _require_school(user)

    webinars = db.scalars(
        select(Webinar)
        .join(PortalMapping, PortalMapping.webinar_id == Webinar.id)
        .options(selectinload(Webinar.workshop))
        .where(PortalMapping.school_id == effective_school_id, Webinar.cycle_id == cycle_id)
        .order_by(Webinar.start_datetime.asc().nullslast())
    ).all()

    items = db.scalars(
        select(CommunicationScheduleItem)
        .options(selectinload(CommunicationScheduleItem.template))
        .where(
            CommunicationScheduleItem.school_id == effective_school_id,
            CommunicationScheduleItem.cycle_id == cycle_id,
        )
    ).all()

    # Admin-set default dates for communication templates in this cycle
    template_defaults = db.scalars(
        select(CommunicationTemplateDefaultDate)
        .options(selectinload(CommunicationTemplateDefaultDate.template))
        .where(CommunicationTemplateDefaultDate.cycle_id == cycle_id)
    ).all()

    return {
        "webinars": [
            WebinarCalendarItem(
                webinar_id=w.id,
                webinar_name=w.webinar_name,
                workshop_name=w.workshop.name if w.workshop else None,
                start_datetime=w.start_datetime,
                end_datetime=w.end_datetime,
                cycle_id=w.cycle_id,
            )
            for w in webinars
        ],
        "schedule_items": [_serialize_schedule_item(i) for i in items],
        "template_defaults": [
            {
                "id": td.id,
                "template_id": td.template_id,
                "template_name": td.template.name if td.template else None,
                "cycle_id": td.cycle_id,
                "suggested_at": td.suggested_at,
                "notes": td.notes,
                "created_at": td.created_at,
                "updated_at": td.updated_at,
            }
            for td in template_defaults
        ],
    }


@router.post("/schedule", response_model=ScheduleItemOut, status_code=status.HTTP_201_CREATED)
def create_schedule_item(
    body: ScheduleItemCreate,
    db: DbDep,
    user: CounselorDep,
) -> dict:
    """Create an explicit schedule item (announcement/followup override, or communication date)."""
    school_id = _require_school(user)

    item = CommunicationScheduleItem(
        school_id=school_id,
        cycle_id=body.cycle_id,
        event_type=body.event_type,
        webinar_id=body.webinar_id,
        template_id=body.template_id,
        scheduled_at=body.scheduled_at,
        is_auto_generated=body.is_auto_generated,
        notes=body.notes,
    )
    db.add(item)
    db.commit()
    # Re-fetch with template loaded for serialization
    item = db.scalar(
        select(CommunicationScheduleItem)
        .options(selectinload(CommunicationScheduleItem.template))
        .where(CommunicationScheduleItem.id == item.id)
    )
    return _serialize_schedule_item(item)


@router.patch("/schedule/{item_id}", response_model=ScheduleItemOut)
def update_schedule_item(
    item_id: uuid.UUID,
    body: ScheduleItemUpdate,
    db: DbDep,
    user: CounselorDep,
) -> dict:
    """Update scheduled_at or notes; automatically marks item as manually set."""
    school_id = _require_school(user)

    item = db.scalar(
        select(CommunicationScheduleItem)
        .options(selectinload(CommunicationScheduleItem.template))
        .where(
            CommunicationScheduleItem.id == item_id,
            CommunicationScheduleItem.school_id == school_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Schedule item not found")

    if body.scheduled_at is not None:
        item.scheduled_at = body.scheduled_at
    if body.is_auto_generated is not None:
        item.is_auto_generated = body.is_auto_generated
    if body.notes is not None:
        item.notes = body.notes
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return _serialize_schedule_item(item)


@router.delete("/schedule/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule_item(
    item_id: uuid.UUID,
    db: DbDep,
    user: CounselorDep,
) -> None:
    """Delete a schedule item. For announcement/followup, this reverts to the computed default."""
    school_id = _require_school(user)

    item = db.scalar(
        select(CommunicationScheduleItem).where(
            CommunicationScheduleItem.id == item_id,
            CommunicationScheduleItem.school_id == school_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Schedule item not found")

    db.delete(item)
    db.commit()


# ── Admin: template default dates (static prefix, must come before /{id}) ─────


@router.get("/templates/{template_id}/default-dates", response_model=list[TemplateDefaultDateOut])
def list_template_default_dates(
    template_id: uuid.UUID,
    db: DbDep,
    _admin: AdminDep,
) -> list:
    """List all per-cycle default dates for a communication template."""
    rows = db.scalars(
        select(CommunicationTemplateDefaultDate)
        .options(selectinload(CommunicationTemplateDefaultDate.template))
        .where(CommunicationTemplateDefaultDate.template_id == template_id)
        .order_by(CommunicationTemplateDefaultDate.cycle_id)
    ).all()
    return [
        {
            "id": r.id, "template_id": r.template_id,
            "template_name": r.template.name if r.template else None,
            "cycle_id": r.cycle_id, "suggested_at": r.suggested_at,
            "notes": r.notes, "created_at": r.created_at, "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.put(
    "/templates/{template_id}/default-dates/{cycle_id}",
    response_model=TemplateDefaultDateOut,
)
def upsert_template_default_date(
    template_id: uuid.UUID,
    cycle_id: uuid.UUID,
    body: TemplateDefaultDateUpsert,
    db: DbDep,
    _admin: AdminDep,
) -> dict:
    """Create or update the suggested send date for a template+cycle (admin only)."""
    existing = db.scalar(
        select(CommunicationTemplateDefaultDate).where(
            CommunicationTemplateDefaultDate.template_id == template_id,
            CommunicationTemplateDefaultDate.cycle_id == cycle_id,
        )
    )
    if existing:
        existing.suggested_at = body.suggested_at
        existing.notes = body.notes
        existing.updated_at = datetime.now(timezone.utc)
    else:
        existing = CommunicationTemplateDefaultDate(
            template_id=template_id,
            cycle_id=cycle_id,
            suggested_at=body.suggested_at,
            notes=body.notes,
        )
        db.add(existing)
    db.commit()
    # Re-fetch with template loaded
    row = db.scalar(
        select(CommunicationTemplateDefaultDate)
        .options(selectinload(CommunicationTemplateDefaultDate.template))
        .where(CommunicationTemplateDefaultDate.id == existing.id)
    )
    return {
        "id": row.id, "template_id": row.template_id,
        "template_name": row.template.name if row.template else None,
        "cycle_id": row.cycle_id, "suggested_at": row.suggested_at,
        "notes": row.notes, "created_at": row.created_at, "updated_at": row.updated_at,
    }


@router.delete("/templates/{template_id}/default-dates/{cycle_id}", status_code=204)
def delete_template_default_date(
    template_id: uuid.UUID,
    cycle_id: uuid.UUID,
    db: DbDep,
    _admin: AdminDep,
) -> None:
    """Remove the suggested send date for a template+cycle (admin only)."""
    row = db.scalar(
        select(CommunicationTemplateDefaultDate).where(
            CommunicationTemplateDefaultDate.template_id == template_id,
            CommunicationTemplateDefaultDate.cycle_id == cycle_id,
        )
    )
    if row:
        db.delete(row)
        db.commit()


# ── Template CRUD (parameterized routes — must come after all static paths) ───


@router.get("/{template_id}", response_model=CommunicationTemplateOut)
def get_template(
    template_id: uuid.UUID,
    db: DbDep,
    user: CounselorDep,
) -> CommunicationTemplate:
    """Get one template. Admins can see inactive templates; counselors cannot."""
    stmt = select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    template = db.execute(stmt).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    # Non-admins cannot see inactive templates
    if not template.is_active and user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    return template


@router.post("/", response_model=CommunicationTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    body: CommunicationTemplateCreate,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Create a new communication template (admin only)."""
    _validate_format(body.format, body.content, body.google_docs_url)

    template = CommunicationTemplate(
        name=body.name,
        description=body.description,
        subject=body.subject,
        format=body.format,
        content=body.content,
        google_docs_url=body.google_docs_url,
        sort_order=body.sort_order,
        updated_at=None,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.patch("/{template_id}/toggle", response_model=CommunicationTemplateOut)
def toggle_template(
    template_id: uuid.UUID,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Flip is_active on a template (admin only). Must be defined before PATCH /{template_id}."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    template.is_active = not template.is_active
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


@router.patch("/{template_id}", response_model=CommunicationTemplateOut)
def update_template(
    template_id: uuid.UUID,
    body: CommunicationTemplateUpdate,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Partial update of a communication template (admin only)."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    # Apply only explicitly set fields
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(template, field, value)

    # Re-validate format consistency if format or content/url changed
    effective_format = updates.get("format", template.format)
    effective_content = updates.get("content", template.content)
    effective_url = updates.get("google_docs_url", template.google_docs_url)
    _validate_format(effective_format, effective_content, effective_url)

    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: uuid.UUID,
    db: DbDep,
    _user: AdminDep,
) -> None:
    """Hard delete a communication template (admin only)."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    db.delete(template)
    db.commit()
