"""Workshop, webinar, and registration endpoints (admin + public)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.integrations import zoom as zoom_client
from src.utils.tiptap import extract_text
from src.content.models import ContentAsset, WorkshopResource, Objective
from src.cycles.models import Cycle
from src.content.schemas import ContentAssetSummary
from src.schools.models import School
from src.workshops.models import PortalMapping, Webinar, Workshop, WorkshopRegistration
from src.workshops.schemas import (
    ObjectiveIdsBody,
    ObjectiveSummary,
    PortalMappingCreate,
    PortalMappingOut,
    RegistrationCreate,
    RegistrationOut,
    RegistrationUpdate,
    SchoolWorkshopsResponse,
    WebinarCreate,
    WebinarOut,
    WebinarSummary,
    WebinarUpdate,
    WorkshopCreate,
    WorkshopOut,
    WorkshopPortalItem,
    WorkshopResourcesUpdate,
    WorkshopSummary,
    WorkshopUpdate,
)

router = APIRouter(prefix="/api/v1/workshops", tags=["workshops"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _webinar_out(webinar: Webinar) -> WebinarOut:
    return WebinarOut(
        id=webinar.id,
        workshop_id=webinar.workshop_id,
        cohort_id=webinar.cohort_id,
        cycle_id=webinar.cycle_id,
        webinar_name=webinar.webinar_name,
        zoom_webinar_id=webinar.zoom_webinar_id,
        start_datetime=webinar.start_datetime,
        end_datetime=webinar.end_datetime,
        duration_minutes=webinar.duration_minutes,
        join_url=webinar.join_url,
        start_url=webinar.start_url,
        registration_url=webinar.registration_url,
        zoom_link=webinar.zoom_link,
        video_embed_code=webinar.video_embed_code,
        audio_transcript=webinar.audio_transcript,
        track_registrations=webinar.track_registrations,
        created_at=webinar.created_at,
        workshop_name=webinar.workshop.name,
        cohort_name=webinar.cohort.name if webinar.cohort else None,
        registration_count=len(webinar.registrations),
    )


def _apply_zoom_details(data: dict, zoom: dict, *, use_setdefault: bool = False) -> None:
    """Populate webinar fields from a Zoom API webinar response dict.

    When ``use_setdefault`` is True (update path), only fills in keys that are
    not already present in ``data`` — so explicit admin overrides always win.
    When False (create path), uses ``if not data.get(...)`` to skip non-empty values.
    """
    def _set(key: str, value: object) -> None:
        if value is None:
            return
        if use_setdefault:
            data.setdefault(key, value)
        elif not data.get(key):
            data[key] = value

    _set("join_url", zoom.get("join_url"))
    _set("start_url", zoom.get("start_url"))
    _set("zoom_link", zoom.get("join_url"))
    _set("registration_url", zoom.get("registration_url"))
    _set("webinar_name", zoom.get("topic"))

    # Parse start_time (ISO 8601, Zoom uses UTC "Z" suffix)
    start_str: str | None = zoom.get("start_time")
    if start_str:
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            _set("start_datetime", start_dt)
            duration_min: int | None = zoom.get("duration")
            if duration_min:
                _set("end_datetime", start_dt + timedelta(minutes=duration_min))
        except ValueError:
            pass


def _registration_out(reg: WorkshopRegistration) -> RegistrationOut:
    return RegistrationOut(
        id=reg.id,
        webinar_id=reg.webinar_id,
        school_id=reg.school_id,
        first_name=reg.first_name,
        last_name=reg.last_name,
        full_name=reg.full_name,
        email=reg.email,
        grade=reg.grade,
        status=reg.status,
        attended=reg.attended,
        join_time=reg.join_time,
        leave_time=reg.leave_time,
        zoom_registrant_id=reg.zoom_registrant_id,
        questions=reg.questions,
        registration_time=reg.registration_time,
        created_at=reg.created_at,
        school_name=reg.school.name if reg.school else None,
    )




def _to_item(
    mapping: PortalMapping,
    prev_cycle_video_embed_code: str | None = None,
    prev_cycle_name: str | None = None,
) -> WorkshopPortalItem:
    webinar: Webinar = mapping.webinar
    workshop: Workshop = webinar.workshop
    return WorkshopPortalItem(
        webinar_id=webinar.id,
        start_datetime=webinar.start_datetime,
        end_datetime=webinar.end_datetime,
        registration_url=webinar.registration_url,
        zoom_link=webinar.zoom_link,
        video_embed_code=webinar.video_embed_code,
        join_url=webinar.join_url,
        show_zoom=mapping.show_zoom,
        workshop_id=workshop.id,
        name=workshop.name,
        description=workshop.description,
        key_actions=workshop.key_actions,
        body=workshop.body,
        suggested_grades=workshop.suggested_grades,
        workshop_art_url=workshop.workshop_art_url,
        sequence_number=workshop.sequence_number,
        action_items=list(workshop.action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in workshop.content_assets if a.status == "published"],
        cycle_name=webinar.cycle.name if webinar.cycle else None,
        prev_cycle_video_embed_code=prev_cycle_video_embed_code,
        prev_cycle_name=prev_cycle_name,
    )


# ── Admin: Webinars (literal prefix — registered before /{workshop_id}) ─────


@router.get("/webinars/{webinar_id}", response_model=WebinarOut)
def get_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(Webinar)
        .where(Webinar.id == webinar_id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cohort), selectinload(Webinar.registrations))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")
    return _webinar_out(obj)


@router.patch("/webinars/{webinar_id}", response_model=WebinarOut)
def update_webinar(webinar_id: uuid.UUID, body: WebinarUpdate, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(Webinar)
        .where(Webinar.id == webinar_id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cohort), selectinload(Webinar.registrations))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")
    update_data = body.model_dump(exclude_unset=True)

    # Auto-populate fields from Zoom API when zoom_webinar_id is being set
    if "zoom_webinar_id" in update_data and update_data["zoom_webinar_id"]:
        zoom_details = zoom_client.get_webinar(update_data["zoom_webinar_id"])
        if zoom_details:
            _apply_zoom_details(update_data, zoom_details, use_setdefault=True)

    for k, v in update_data.items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return _webinar_out(obj)


@router.delete("/webinars/{webinar_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Webinar, webinar_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")
    db.delete(obj)
    db.commit()


@router.get("/webinars/{webinar_id}/registrations", response_model=list[RegistrationOut])
def list_registrations(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")
    regs = db.execute(
        select(WorkshopRegistration)
        .where(WorkshopRegistration.webinar_id == webinar_id)
        .options(selectinload(WorkshopRegistration.school))
        .order_by(WorkshopRegistration.created_at)
    ).scalars().all()
    return [_registration_out(r) for r in regs]


@router.post("/webinars/{webinar_id}/registrations", response_model=RegistrationOut, status_code=status.HTTP_201_CREATED)
def create_registration(webinar_id: uuid.UUID, body: RegistrationCreate, _admin: AdminDep, db: DbDep):
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")
    obj = WorkshopRegistration(webinar_id=webinar_id, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    obj = db.execute(
        select(WorkshopRegistration)
        .where(WorkshopRegistration.id == obj.id)
        .options(selectinload(WorkshopRegistration.school))
    ).scalar_one()
    return _registration_out(obj)


# ── Admin: Portal mapping (literal prefix) ───────────────────────────────────


@router.get("/webinars/{webinar_id}/schools", response_model=list[PortalMappingOut])
def list_webinar_schools(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: list schools that have this webinar in their portal."""
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")
    mappings = db.execute(
        select(PortalMapping)
        .where(PortalMapping.webinar_id == webinar_id)
        .options(selectinload(PortalMapping.school))
        .order_by(PortalMapping.created_at)
    ).scalars().all()
    return [
        PortalMappingOut(
            id=m.id,
            school_id=m.school_id,
            school_name=m.school.name,
            webinar_id=m.webinar_id,
            show_zoom=m.show_zoom,
            created_at=m.created_at,
        )
        for m in mappings
    ]


@router.post("/webinars/{webinar_id}/schools", response_model=PortalMappingOut, status_code=status.HTTP_201_CREATED)
def add_webinar_school(webinar_id: uuid.UUID, body: PortalMappingCreate, _admin: AdminDep, db: DbDep):
    """Admin: add a school to a webinar's portal mapping."""
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")
    school = db.get(School, body.school_id)
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    mapping = PortalMapping(school_id=body.school_id, webinar_id=webinar_id, show_zoom=body.show_zoom)
    db.add(mapping)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="School is already mapped to this webinar")
    db.refresh(mapping)
    return PortalMappingOut(
        id=mapping.id,
        school_id=mapping.school_id,
        school_name=school.name,
        webinar_id=mapping.webinar_id,
        show_zoom=mapping.show_zoom,
        created_at=mapping.created_at,
    )


@router.delete("/webinars/{webinar_id}/schools/{school_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_webinar_school(webinar_id: uuid.UUID, school_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: remove a school from a webinar's portal mapping."""
    mapping = db.execute(
        select(PortalMapping).where(
            PortalMapping.webinar_id == webinar_id,
            PortalMapping.school_id == school_id,
        )
    ).scalar_one_or_none()
    if not mapping:
        raise HTTPException(status_code=404, detail="School is not mapped to this webinar")
    db.delete(mapping)
    db.commit()


# ── Admin: Registrations (literal prefix) ────────────────────────────────────


@router.patch("/registrations/{registration_id}", response_model=RegistrationOut)
def update_registration(registration_id: uuid.UUID, body: RegistrationUpdate, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(WorkshopRegistration)
        .where(WorkshopRegistration.id == registration_id)
        .options(selectinload(WorkshopRegistration.school))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Registration not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return _registration_out(obj)


@router.delete("/registrations/{registration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_registration(registration_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(WorkshopRegistration, registration_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Registration not found")
    db.delete(obj)
    db.commit()


# ── Public endpoints (literal prefix) ───────────────────────────────────────


def _get_prev_cycle_recording(workshop_id: uuid.UUID, db: DbDep) -> tuple[str | None, str | None]:
    """Return (video_embed_code, cycle_name) for the most recent past webinar
    of the given workshop that has a recording. Returns (None, None) if none found."""
    row = db.execute(
        select(Webinar)
        .where(
            Webinar.workshop_id == workshop_id,
            Webinar.video_embed_code.isnot(None),
            Webinar.video_embed_code != "",
        )
        .options(selectinload(Webinar.cycle))
        .order_by(Webinar.start_datetime.desc().nulls_last())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None, None
    return row.video_embed_code, (row.cycle.name if row.cycle else None)


@router.get("/public/school/{school_id}", response_model=SchoolWorkshopsResponse)
def get_school_workshops(school_id: uuid.UUID, db: DbDep) -> SchoolWorkshopsResponse:
    """Return upcoming and past workshops for a school portal (no auth)."""
    mappings = (
        db.execute(
            select(PortalMapping)
            .where(PortalMapping.school_id == school_id)
            .options(
                selectinload(PortalMapping.webinar).options(
                    selectinload(Webinar.workshop).selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
                    selectinload(Webinar.cycle),
                )
            )
            .order_by(PortalMapping.created_at)
        )
        .scalars()
        .all()
    )

    now = datetime.now(tz=timezone.utc)
    upcoming: list[WorkshopPortalItem] = []
    past: list[WorkshopPortalItem] = []

    for mapping in mappings:
        webinar = mapping.webinar
        is_upcoming = webinar.start_datetime is None or webinar.start_datetime >= now
        # Past: only include webinars from the current cycle (or with no cycle set)
        if not is_upcoming:
            cycle = webinar.cycle
            if cycle is not None and not cycle.is_current:
                continue

        if is_upcoming:
            prev_embed, prev_name = _get_prev_cycle_recording(webinar.workshop_id, db)
            item = _to_item(mapping, prev_cycle_video_embed_code=prev_embed, prev_cycle_name=prev_name)
            upcoming.append(item)
        else:
            item = _to_item(mapping)
            past.append(item)

    # Sort upcoming ascending (soonest first), past descending (most recent first)
    upcoming.sort(key=lambda x: x.start_datetime or datetime.max.replace(tzinfo=timezone.utc))
    past.sort(key=lambda x: x.start_datetime or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return SchoolWorkshopsResponse(upcoming=upcoming, past=past)


@router.get("/public/school/{school_id}/webinar/{webinar_id}", response_model=WorkshopPortalItem)
def get_school_webinar(school_id: uuid.UUID, webinar_id: uuid.UUID, db: DbDep) -> WorkshopPortalItem:
    """Return a single webinar's portal details for a school (no auth)."""
    mapping = (
        db.execute(
            select(PortalMapping)
            .where(
                PortalMapping.school_id == school_id,
                PortalMapping.webinar_id == webinar_id,
            )
            .options(
                selectinload(PortalMapping.webinar).options(
                    selectinload(Webinar.workshop).selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
                    selectinload(Webinar.cycle),
                )
            )
        )
        .scalar_one_or_none()
    )
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workshop not found")

    webinar = mapping.webinar
    now = datetime.now(tz=timezone.utc)
    is_upcoming = webinar.start_datetime is None or webinar.start_datetime >= now
    if is_upcoming:
        prev_embed, prev_name = _get_prev_cycle_recording(webinar.workshop_id, db)
        return _to_item(mapping, prev_cycle_video_embed_code=prev_embed, prev_cycle_name=prev_name)
    return _to_item(mapping)


@router.post("/public/webinars/{webinar_id}/register", response_model=RegistrationOut, status_code=status.HTTP_201_CREATED)
def register_public(webinar_id: uuid.UUID, body: RegistrationCreate, db: DbDep) -> RegistrationOut:
    """
    Public registration for a webinar (no auth required).

    Creates a registration with 'approved' status and current timestamp.
    If the user is already registered (same email + webinar), returns the existing registration.
    """
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webinar not found")

    # Check if user is already registered (by email)
    existing = db.execute(
        select(WorkshopRegistration)
        .where(
            WorkshopRegistration.webinar_id == webinar_id,
            WorkshopRegistration.email == body.email,
        )
        .options(selectinload(WorkshopRegistration.school))
    ).scalar_one_or_none()

    if existing:
        # Return existing registration (idempotent)
        return _registration_out(existing)

    # Create new registration
    reg_data = body.model_dump()
    reg_data["status"] = "approved"  # Auto-approve public registrations
    reg_data["registration_time"] = datetime.now(tz=timezone.utc)

    obj = WorkshopRegistration(webinar_id=webinar_id, **reg_data)
    db.add(obj)
    db.commit()
    db.refresh(obj)

    # Register on Zoom if this webinar has a Zoom ID (non-fatal if it fails)
    if webinar.zoom_webinar_id:
        zoom_registrant_id = zoom_client.register_webinar(
            zoom_webinar_id=webinar.zoom_webinar_id,
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            questions=body.questions,
        )
        if zoom_registrant_id:
            obj.zoom_registrant_id = zoom_registrant_id
            db.commit()

    # Reload with school relationship
    obj = db.execute(
        select(WorkshopRegistration)
        .where(WorkshopRegistration.id == obj.id)
        .options(selectinload(WorkshopRegistration.school))
    ).scalar_one()

    return _registration_out(obj)


# ── Admin: Workshops (parameterised paths — registered last) ─────────────────


@router.get("/", response_model=list[WorkshopSummary])
def list_workshops(_admin: AdminDep, db: DbDep):
    """Admin: list all workshops with webinar counts and next upcoming date."""
    now = datetime.now(tz=timezone.utc)

    webinar_count_sq = (
        select(func.count(Webinar.id))
        .where(Webinar.workshop_id == Workshop.id)
        .correlate(Workshop)
        .scalar_subquery()
    )
    next_webinar_sq = (
        select(func.min(Webinar.start_datetime))
        .where(Webinar.workshop_id == Workshop.id, Webinar.start_datetime >= now)
        .correlate(Workshop)
        .scalar_subquery()
    )

    stmt = (
        select(
            Workshop,
            webinar_count_sq.label("webinar_count"),
            next_webinar_sq.label("next_webinar_date"),
        )
        .order_by(Workshop.sequence_number.nulls_last(), Workshop.name)
    )

    rows = db.execute(stmt).all()
    return [
        WorkshopSummary(
            id=row.Workshop.id,
            name=row.Workshop.name,
            description=row.Workshop.description,
            suggested_grades=row.Workshop.suggested_grades,
            workshop_art_url=row.Workshop.workshop_art_url,
            sequence_number=row.Workshop.sequence_number,
            created_at=row.Workshop.created_at,
            webinar_count=row.webinar_count,
            next_webinar_date=row.next_webinar_date,
        )
        for row in rows
    ]


@router.post("/", response_model=WorkshopOut, status_code=status.HTTP_201_CREATED)
def create_workshop(body: WorkshopCreate, _admin: AdminDep, db: DbDep):
    obj = Workshop(**body.model_dump())
    obj.search_text = " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.body),
        extract_text(obj.key_actions),
    ]))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return WorkshopOut(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        key_actions=obj.key_actions,
        body=obj.body,
        sequence_number=obj.sequence_number,
        suggested_grades=obj.suggested_grades,
        resource_center_slug=obj.resource_center_slug,
        workshop_art_url=obj.workshop_art_url,
        created_at=obj.created_at,
        webinar_count=0,
    )


@router.get("/{workshop_id}", response_model=WorkshopOut)
def get_workshop(workshop_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: get workshop detail (webinars loaded separately via /webinars endpoint)."""
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")
    return WorkshopOut(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        key_actions=obj.key_actions,
        body=obj.body,
        sequence_number=obj.sequence_number,
        suggested_grades=obj.suggested_grades,
        resource_center_slug=obj.resource_center_slug,
        workshop_art_url=obj.workshop_art_url,
        created_at=obj.created_at,
        webinar_count=len(obj.webinars),
        objectives=[ObjectiveSummary(id=o.id, name=o.name, description=o.description) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in obj.content_assets],
    )


@router.patch("/{workshop_id}", response_model=WorkshopOut)
def update_workshop(workshop_id: uuid.UUID, body: WorkshopUpdate, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.search_text = " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.body),
        extract_text(obj.key_actions),
    ]))
    db.commit()
    db.refresh(obj)
    return WorkshopOut(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        key_actions=obj.key_actions,
        body=obj.body,
        sequence_number=obj.sequence_number,
        suggested_grades=obj.suggested_grades,
        resource_center_slug=obj.resource_center_slug,
        workshop_art_url=obj.workshop_art_url,
        created_at=obj.created_at,
        webinar_count=len(obj.webinars),
        objectives=[ObjectiveSummary(id=o.id, name=o.name, description=o.description) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in obj.content_assets],
    )


@router.get("/{workshop_id}/webinars", response_model=list[WebinarSummary])
def list_workshop_webinars(
    workshop_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    search: str | None = None,
    status: str | None = None,  # "upcoming", "past", or None for all
    sort: str = "date_desc",  # "date_asc" or "date_desc"
):
    """Admin: list webinars for a workshop with filtering and sorting."""
    workshop = db.get(Workshop, workshop_id)
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    now = datetime.now(tz=timezone.utc)
    stmt = select(Webinar).where(Webinar.workshop_id == workshop_id).options(selectinload(Webinar.registrations))

    # Filter by search term
    if search:
        stmt = stmt.where(Webinar.webinar_name.ilike(f"%{search}%"))

    # Filter by status (upcoming/past)
    if status == "upcoming":
        stmt = stmt.where((Webinar.start_datetime >= now) | (Webinar.start_datetime.is_(None)))
    elif status == "past":
        stmt = stmt.where(Webinar.start_datetime < now)

    # Sort by date
    if sort == "date_asc":
        stmt = stmt.order_by(Webinar.start_datetime.asc().nulls_last())
    else:
        stmt = stmt.order_by(Webinar.start_datetime.desc().nulls_last())

    webinars = db.execute(stmt).scalars().all()
    return [
        WebinarSummary(
            id=w.id,
            webinar_name=w.webinar_name,
            cohort_id=w.cohort_id,
            start_datetime=w.start_datetime,
            end_datetime=w.end_datetime,
            zoom_webinar_id=w.zoom_webinar_id,
            registration_url=w.registration_url,
            zoom_link=w.zoom_link,
            registration_count=len(w.registrations),
        )
        for w in webinars
    ]


@router.put("/{workshop_id}/objectives", response_model=WorkshopOut)
def update_workshop_objectives(
    workshop_id: uuid.UUID,
    body: ObjectiveIdsBody,
    _admin: AdminDep,
    db: DbDep,
):
    """Admin: replace the full set of objectives for a workshop."""
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")

    new_objectives = db.execute(
        select(Objective).where(Objective.id.in_(body.ids))
    ).scalars().all() if body.ids else []

    obj.objectives = list(new_objectives)
    db.commit()
    db.refresh(obj)
    # Reload after commit
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one()
    return WorkshopOut(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        key_actions=obj.key_actions,
        body=obj.body,
        sequence_number=obj.sequence_number,
        suggested_grades=obj.suggested_grades,
        resource_center_slug=obj.resource_center_slug,
        workshop_art_url=obj.workshop_art_url,
        created_at=obj.created_at,
        webinar_count=len(obj.webinars),
        objectives=[ObjectiveSummary(id=o.id, name=o.name, description=o.description) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in obj.content_assets],
    )


@router.put("/{workshop_id}/resources", response_model=WorkshopOut)
def update_workshop_resources(
    workshop_id: uuid.UUID,
    body: WorkshopResourcesUpdate,
    _admin: AdminDep,
    db: DbDep,
):
    """Admin: replace the full set of linked resources for a workshop (with sort order)."""
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(selectinload(Workshop.webinars), selectinload(Workshop.objectives))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")

    # Delete existing junction rows and re-insert with new sort order
    db.execute(
        WorkshopResource.__table__.delete().where(
            WorkshopResource.workshop_id == workshop_id
        )
    )
    for item in body.items:
        db.execute(
            WorkshopResource.__table__.insert().values(
                content_asset_id=item.content_asset_id,
                workshop_id=workshop_id,
                sort_order=item.sort_order,
            )
        )
    db.commit()

    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one()
    return WorkshopOut(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        key_actions=obj.key_actions,
        body=obj.body,
        sequence_number=obj.sequence_number,
        suggested_grades=obj.suggested_grades,
        resource_center_slug=obj.resource_center_slug,
        workshop_art_url=obj.workshop_art_url,
        created_at=obj.created_at,
        webinar_count=len(obj.webinars),
        objectives=[ObjectiveSummary(id=o.id, name=o.name, description=o.description) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in obj.content_assets],
    )


@router.delete("/{workshop_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workshop(workshop_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Workshop, workshop_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")
    db.delete(obj)
    db.commit()


@router.post("/{workshop_id}/webinars", response_model=WebinarOut, status_code=status.HTTP_201_CREATED)
def create_webinar(workshop_id: uuid.UUID, body: WebinarCreate, _admin: AdminDep, db: DbDep):
    workshop = db.get(Workshop, workshop_id)
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    # Create the webinar (workshop_id from URL, exclude school_ids — not a model field)
    webinar_data = body.model_dump(exclude={"school_ids"})
    webinar_data["workshop_id"] = workshop_id

    # Auto-populate fields from Zoom API when a zoom_webinar_id is provided
    if webinar_data.get("zoom_webinar_id"):
        zoom_details = zoom_client.get_webinar(webinar_data["zoom_webinar_id"])
        if zoom_details:
            _apply_zoom_details(webinar_data, zoom_details)

    obj = Webinar(**webinar_data)
    db.add(obj)
    db.flush()  # get obj.id without committing

    # Create portal_mapping entries for all selected schools
    for school_id in body.school_ids:
        mapping = PortalMapping(school_id=school_id, webinar_id=obj.id, show_zoom=True)
        db.add(mapping)

    db.commit()
    db.refresh(obj)

    # Eager-load relationships for the response
    obj = db.execute(
        select(Webinar)
        .where(Webinar.id == obj.id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cohort), selectinload(Webinar.registrations))
    ).scalar_one()
    return _webinar_out(obj)
