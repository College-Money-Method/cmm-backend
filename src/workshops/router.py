"""Workshop, webinar, and registration endpoints (admin + public)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import cast, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from src.auth.deps import AdminDep, CounselorDep, CurrentUserDep
from src.db.deps import DbDep
from src.integrations import zoom as zoom_client
from src.utils.tiptap import extract_text
from src.content.models import ContentAsset, WorkshopResource, Objective, ObjectiveWorkshop
from src.cycles.models import Cycle
from src.emails.automation_rearm import rearm_automations_for_webinar
from src.emails.models import EmailSendLog
from src.content.schemas import ContentAssetSummary
from src.schools.models import School
from src.workshops.models import AirtableSyncLog, PortalMapping, Webinar, Workshop, WorkshopEmailTemplate, WorkshopNotificationSubscriber, WorkshopRegistration
from src.workshops import attendance_sync_service
from src.workshops.schemas import (
    AirtableSyncLogOut,
    AirtableSyncResult,
    EmailTemplateCreate,
    EmailTemplateOut,
    EmailTemplateUpdate,
    NotificationSubscribeRequest,
    NotificationSubscriberOut,
    ObjectiveIdsBody,
    ObjectiveSummary,
    PortalMappingCreate,
    PortalMappingOut,
    PortalMappingOverrideUpdate,
    RegistrationCreate,
    RegistrationOut,
    RegistrationUpdate,
    SchoolWorkshopsResponse,
    WebinarCreate,
    WebinarListItem,
    WebinarOut,
    WebinarSummary,
    WebinarUpdate,
    WorkshopCreate,
    WorkshopObjectiveWithResources,
    WorkshopOut,
    WorkshopPortalItem,
    WorkshopResourcesUpdate,
    WorkshopSummary,
    WorkshopUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workshops", tags=["workshops"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _delete_impact(db, webinar_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    """``{webinar_id: (school_count, email_send_count)}`` for a batch of webinars.

    Two grouped queries rather than per-row counts: the Sessions list renders
    every webinar in a cycle, and a delete prompt on each row must not cost a
    query per row.

    Both numbers answer "is this webinar real?" for the delete confirmation.
    `portal_mapping` rows cascade away with the webinar; `email_send_log` rows
    survive (ON DELETE SET NULL) but lose their linkage, so mail that already
    went out becomes unattributable.
    """
    if not webinar_ids:
        return {}
    schools = dict(
        db.execute(
            select(PortalMapping.webinar_id, func.count())
            .where(PortalMapping.webinar_id.in_(webinar_ids))
            .group_by(PortalMapping.webinar_id)
        ).all()
    )
    sends = dict(
        db.execute(
            select(EmailSendLog.webinar_id, func.count())
            .where(EmailSendLog.webinar_id.in_(webinar_ids))
            .group_by(EmailSendLog.webinar_id)
        ).all()
    )
    return {wid: (schools.get(wid, 0), sends.get(wid, 0)) for wid in webinar_ids}


def _webinar_out(webinar: Webinar, db=None, rearmed_automation_sends: int = 0) -> WebinarOut:
    school_count, email_send_count = (
        _delete_impact(db, [webinar.id]).get(webinar.id, (0, 0)) if db is not None else (0, 0)
    )
    return WebinarOut(
        school_count=school_count,
        email_send_count=email_send_count,
        rearmed_automation_sends=rearmed_automation_sends,
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
        attendance_synced_at=webinar.attendance_synced_at,
        created_at=webinar.created_at,
        workshop_name=webinar.workshop.name,
        cohort_name=webinar.cohort.name if webinar.cohort else None,
        registration_count=len(webinar.registrations),
        slug=webinar.slug,
        previous_start_datetime=webinar.previous_start_datetime,
        rescheduled_at=webinar.rescheduled_at,
    )


# A start time that moves by less than this is a correction, not a reschedule.
# The distinction decides who gets emailed: a material move re-arms the workshop
# automations, so every mapped counselor receives the reminder again. One hour
# absorbs timezone nudges and minor fixes without doing that.
MATERIAL_RESCHEDULE_DELTA = timedelta(hours=1)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Comparable UTC datetime. `start_datetime` is stored with a timezone, but a
    client can still post an offset-less ISO string, which pydantic keeps naive —
    and comparing naive to aware raises."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _validate_webinar_schedule(obj: Webinar, requested: dict, *, allow_past: bool) -> bool:
    """Guard a webinar datetime change; report whether it is a real reschedule.

    ``requested`` must hold only the fields the client explicitly sent. Values
    filled in from Zoom are deliberately excluded, and the Airtable sync
    (``sync_webinars.py``) writes the ORM directly rather than coming through
    this endpoint — both are backfill of what already happened, so neither
    should be refused for being historical.

    Two operations look identical in the payload and must not be conflated:
    correcting a wrong recorded date (past, legitimate, must not email anyone)
    and moving a session (future, must re-arm automations). The caller's
    ``allow_past`` flag is what separates them — never a guess from the dates.
    """
    now = datetime.now(timezone.utc)
    old_start = _as_utc(obj.start_datetime)
    new_start = _as_utc(requested.get("start_datetime")) if "start_datetime" in requested else old_start
    new_end = _as_utc(requested["end_datetime"]) if "end_datetime" in requested else _as_utc(obj.end_datetime)
    changing_start = "start_datetime" in requested and new_start != old_start

    if changing_start and new_start is not None and new_start < now and not allow_past:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That start time is already in the past. Confirm you are correcting a "
                "historical date rather than rescheduling, and save again."
            ),
        )

    # duration_minutes is a generated column computed from the pair, so an
    # inverted pair silently stores a negative duration today.
    if new_start is not None and new_end is not None and new_end <= new_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The end time must be after the start time.",
        )

    return bool(
        changing_start
        and not allow_past
        and old_start is not None  # first-time scheduling is not a reschedule
        and new_start is not None
        and new_start > now  # moving into the past would only re-fire follow-ups
        and abs(new_start - old_start) > MATERIAL_RESCHEDULE_DELTA
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


def _objective_with_resources(
    obj: Objective,
    *,
    published_only: bool = False,
) -> WorkshopObjectiveWithResources:
    assets = obj.content_assets
    if published_only:
        assets = [a for a in assets if a.status == "published"]
    return WorkshopObjectiveWithResources(
        id=obj.id,
        name=obj.name,
        description=obj.description,
        resources=[ContentAssetSummary.model_validate(a) for a in assets],
    )


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
        zoom_registrant_id=str(reg.zoom_registrant_id) if reg.zoom_registrant_id is not None else None,
        questions=reg.questions,
        registration_time=reg.registration_time,
        created_at=reg.created_at,
        school_name=reg.school.name if reg.school else None,
    )


def _subscriber_out(sub: "WorkshopNotificationSubscriber") -> NotificationSubscriberOut:
    return NotificationSubscriberOut(
        id=sub.id,
        email=sub.email,
        first_name=sub.first_name,
        last_name=sub.last_name,
        school_id=sub.school_id,
        school_name=sub.school.name if sub.school else None,
        cycle_name=sub.cycle_name,
        subscribed_at=sub.subscribed_at,
        notification_types=list(sub.notification_types or []),
    )



def _to_item(
    mapping: PortalMapping,
    prev_cycle_video_embed_code: str | None = None,
    prev_cycle_name: str | None = None,
) -> WorkshopPortalItem:
    webinar: Webinar = mapping.webinar
    workshop: Workshop = webinar.workshop
    override = mapping.school_override or {}
    # Use override value if the key is present (even if null); else fall back to workshop default
    effective_grades = (
        override["suggested_grades"]
        if "suggested_grades" in override
        else workshop.suggested_grades
    )
    school_regs = [r for r in (webinar.registrations or []) if r.school_id == mapping.school_id]
    return WorkshopPortalItem(
        portal_mapping_id=mapping.id,
        school_override=override or None,
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
        suggested_grades=effective_grades,
        workshop_art_url=workshop.workshop_art_url,
        sequence_number=workshop.sequence_number,
        action_items=list(workshop.action_items or []),
        key_action_items=list(workshop.key_action_items or []),
        objectives=[_objective_with_resources(o, published_only=True) for o in workshop.objectives],
        resources=[ContentAssetSummary.model_validate(a) for a in workshop.content_assets if a.status == "published"],
        cycle_name=webinar.cycle.name if webinar.cycle else None,
        prev_cycle_video_embed_code=prev_cycle_video_embed_code,
        prev_cycle_name=prev_cycle_name,
        slug=webinar.slug,
        registration_count=len(school_regs),
        attendee_count=sum(1 for r in school_regs if r.attended),
    )


# ── Admin: Webinars (literal prefix — registered before /{workshop_id}) ─────


@router.get("/webinars", response_model=list[WebinarListItem])
def list_all_webinars(
    _admin: AdminDep,
    db: DbDep,
    search: str | None = None,
    status: str | None = None,  # "upcoming", "past", or None for all
    sort: str = "date_asc",  # "date_asc" or "date_desc"
    school_id: uuid.UUID | None = None,
    workshop_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
    zoom_webinar_id: str | None = None,
):
    """Admin: global webinar list filterable by cycle, school, workshop, status, search, and zoom webinar id."""
    now = datetime.now(tz=timezone.utc)
    stmt = select(Webinar).options(
        selectinload(Webinar.workshop),
        selectinload(Webinar.cohort),
        selectinload(Webinar.cycle),
        selectinload(Webinar.registrations),
    )

    if cycle_id:
        stmt = stmt.where(Webinar.cycle_id == cycle_id)

    if workshop_id:
        stmt = stmt.where(Webinar.workshop_id == workshop_id)

    if school_id:
        # Filter to webinars mapped to this school via portal_mapping
        stmt = stmt.where(
            select(PortalMapping)
            .where(PortalMapping.webinar_id == Webinar.id, PortalMapping.school_id == school_id)
            .exists()
        )

    if search:
        stmt = stmt.where(Webinar.webinar_name.ilike(f"%{search}%"))

    if zoom_webinar_id:
        # Substring match so admins can paste a partial or full zoom webinar id
        stmt = stmt.where(Webinar.zoom_webinar_id.ilike(f"%{zoom_webinar_id}%"))

    if status == "upcoming":
        stmt = stmt.where((Webinar.start_datetime >= now) | (Webinar.start_datetime.is_(None)))
    elif status == "past":
        stmt = stmt.where(Webinar.start_datetime < now)

    stmt = stmt.order_by(
        Webinar.start_datetime.asc().nulls_last()
        if sort == "date_asc"
        else Webinar.start_datetime.desc().nulls_last()
    )

    webinars = db.execute(stmt).scalars().all()
    impact = _delete_impact(db, [w.id for w in webinars])
    return [
        WebinarListItem(
            school_count=impact[w.id][0],
            email_send_count=impact[w.id][1],
            id=w.id,
            webinar_name=w.webinar_name,
            cohort_id=w.cohort_id,
            start_datetime=w.start_datetime,
            end_datetime=w.end_datetime,
            zoom_webinar_id=w.zoom_webinar_id,
            registration_url=w.registration_url,
            zoom_link=w.zoom_link,
            registration_count=len(w.registrations),
            workshop_id=w.workshop_id,
            workshop_name=w.workshop.name,
            cohort_name=w.cohort.name if w.cohort else None,
            cycle_id=w.cycle_id,
            cycle_name=w.cycle.name if w.cycle else None,
            slug=w.slug,
        )
        for w in webinars
    ]


@router.get("/webinars/{webinar_id}", response_model=WebinarOut)
def get_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(Webinar)
        .where(Webinar.id == webinar_id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cohort), selectinload(Webinar.registrations))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")
    return _webinar_out(obj, db)


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
    # Not a webinar column — it only says how to read the datetime change.
    allow_past = bool(update_data.pop("allow_past_datetime", False))
    is_reschedule = _validate_webinar_schedule(obj, update_data, allow_past=allow_past)
    previous_start = obj.start_datetime

    # Auto-populate fields from Zoom API when zoom_webinar_id is being set
    if "zoom_webinar_id" in update_data and update_data["zoom_webinar_id"]:
        zoom_details = zoom_client.get_webinar(update_data["zoom_webinar_id"])
        if zoom_details:
            _apply_zoom_details(update_data, zoom_details, use_setdefault=True)

    for k, v in update_data.items():
        setattr(obj, k, v)

    rearmed = 0
    if is_reschedule:
        # Recorded in the same transaction as the move, so a failed commit
        # cannot leave a webinar claiming a reschedule that did not happen.
        obj.previous_start_datetime = previous_start
        obj.rescheduled_at = datetime.now(timezone.utc)
        # The session moved, so anything already "sent" for the old date has to
        # be re-evaluated against the new one — otherwise counselors keep a date
        # that no longer happens and no further mail is ever sent for it.
        rearmed = rearm_automations_for_webinar(db, obj.id)
    db.commit()
    if is_reschedule:
        # Counselors receiving a second reminder will be asked about, so leave a
        # trail of exactly which move caused it and how much it re-armed.
        logger.info(
            "webinar %s rescheduled from %s to %s; cleared %d automation ledger row(s)",
            obj.id,
            previous_start,
            obj.start_datetime,
            rearmed,
        )
    db.refresh(obj)
    return _webinar_out(obj, db, rearmed_automation_sends=rearmed)


@router.delete("/webinars/{webinar_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep, force: bool = False):
    """Hard-delete a webinar. Blocked by default once the session has history.

    The delete is a *hard* one and the FKs cascade: every
    `workshop_registrations` row for this session — registrations and their
    attendance — is destroyed with it, along with the school `portal_mapping`
    rows and their `automation_send_ledger` entries. There is no soft-cancel
    state to fall back on, so this is unrecoverable.

    That is fine for the case this exists to serve (a webinar created by
    mistake, which has nothing attached). It is not fine for a session families
    registered for or that already generated mail, so those need `force=true`,
    which the admin only reaches through a prompt naming the counts.
    """
    obj = db.get(Webinar, webinar_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")

    if not force:
        registration_count = db.execute(
            select(func.count())
            .select_from(WorkshopRegistration)
            .where(WorkshopRegistration.webinar_id == webinar_id)
        ).scalar_one()
        _, email_send_count = _delete_impact(db, [webinar_id])[webinar_id]
        if registration_count or email_send_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This session has {registration_count} registration(s) and "
                    f"{email_send_count} email(s) already sent. Deleting it destroys "
                    "those registrations and their attendance permanently. Confirm the "
                    "delete to proceed anyway."
                ),
            )

    db.delete(obj)
    db.commit()


@router.post("/webinars/{webinar_id}/sync-attendance", response_model=WebinarOut)
def sync_attendance(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Manually trigger post-webinar attendance sync from Zoom Reports API."""
    obj = db.execute(
        select(Webinar)
        .where(Webinar.id == webinar_id)
        .options(selectinload(Webinar.workshop), selectinload(Webinar.cohort), selectinload(Webinar.registrations))
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Webinar not found")
    if not obj.zoom_webinar_id:
        raise HTTPException(status_code=400, detail="Webinar has no Zoom webinar ID")

    synced = attendance_sync_service.sync_webinar_attendance(obj.zoom_webinar_id, db)
    if not synced:
        raise HTTPException(
            status_code=503,
            detail="Zoom participant report not yet available — try again in a few minutes",
        )

    db.refresh(obj)
    return _webinar_out(obj, db)


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


@router.get("/webinars/{webinar_id}/my-registrations", response_model=list[RegistrationOut])
def list_my_registrations(
    webinar_id: uuid.UUID,
    user: CounselorDep,
    db: DbDep,
    school_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """Counselor-facing registrations list, scoped to the caller's own school.

    Counselors / viewers only see registrations tagged with their school, and only
    for webinars mapped to their school's portal. A super_admin impersonating a
    school passes it via ``school_id`` to get the same per-school scope; without it
    (no impersonation) they see every registration (same as the admin endpoint).
    """
    webinar = db.get(Webinar, webinar_id)
    if not webinar:
        raise HTTPException(status_code=404, detail="Webinar not found")

    stmt = (
        select(WorkshopRegistration)
        .where(WorkshopRegistration.webinar_id == webinar_id)
        .options(selectinload(WorkshopRegistration.school))
        .order_by(WorkshopRegistration.created_at)
    )

    # Determine the school to scope to: counselors/viewers are locked to their own
    # school; a super_admin scopes only when impersonating (school_id passed).
    if user.role == "super_admin":
        scope_school_id = school_id
    else:
        if not user.school_id:
            raise HTTPException(status_code=403, detail="No school associated with your account")
        scope_school_id = user.school_id

    if scope_school_id is not None:
        mapping = db.execute(
            select(PortalMapping).where(
                PortalMapping.webinar_id == webinar_id,
                PortalMapping.school_id == scope_school_id,
            )
        ).scalar_one_or_none()
        if not mapping:
            raise HTTPException(status_code=403, detail="Webinar not in your school's portal")
        stmt = stmt.where(WorkshopRegistration.school_id == scope_school_id)

    regs = db.execute(stmt).scalars().all()
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


# ── Counselor: Portal mapping override ───────────────────────────────────────


@router.patch("/portal-mappings/{portal_mapping_id}", status_code=200)
def update_portal_mapping_override(
    portal_mapping_id: uuid.UUID,
    body: PortalMappingOverrideUpdate,
    user: CounselorDep,
    db: DbDep,
) -> dict:
    """Counselor: shallow-merge override fields into portal_mapping.school_override.
    Only updates the keys present in the request body; other keys are preserved.
    A field sent as null is treated as "reset to default": the key is REMOVED from
    the override so `_to_item` falls back to the CMM-provided workshop default
    (leaving the key with a null value would instead pin the field to blank).
    Verifies the mapping belongs to the counselor's own school.
    """
    mapping = db.get(PortalMapping, portal_mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Portal mapping not found")
    if user.role != "super_admin" and mapping.school_id != user.school_id:
        raise HTTPException(status_code=403, detail="Access restricted to your own school")

    override = dict(mapping.school_override or {})
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            override.pop(field, None)
        else:
            override[field] = value
    mapping.school_override = override
    flag_modified(mapping, "school_override")
    db.commit()
    return {"ok": True}


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


# ── Admin: Notification subscribers (literal prefix) ────────────────────────


@router.get("/notifications", response_model=list[NotificationSubscriberOut])
def list_notification_subscribers(
    _admin: AdminDep,
    db: DbDep,
    school_id: uuid.UUID | None = None,
    cycle_name: str | None = None,
) -> list[NotificationSubscriberOut]:
    """Admin: list notification subscribers, filterable by school and cycle."""
    stmt = (
        select(WorkshopNotificationSubscriber)
        .options(selectinload(WorkshopNotificationSubscriber.school))
        .order_by(WorkshopNotificationSubscriber.subscribed_at.desc())
    )
    if school_id:
        stmt = stmt.where(WorkshopNotificationSubscriber.school_id == school_id)
    if cycle_name:
        stmt = stmt.where(WorkshopNotificationSubscriber.cycle_name == cycle_name)

    rows = db.execute(stmt).scalars().all()
    return [_subscriber_out(r) for r in rows]


# ── Admin: Email templates (literal prefix) ──────────────────────────────────


@router.get("/email-templates", response_model=list[EmailTemplateOut])
def list_email_templates(
    _user: CounselorDep,
    db: DbDep,
    workshop_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[EmailTemplateOut]:
    """Counselor: list email templates. Optionally filtered to a specific workshop."""
    stmt = select(WorkshopEmailTemplate).order_by(WorkshopEmailTemplate.type, WorkshopEmailTemplate.name)
    if workshop_id is not None:
        stmt = stmt.where(WorkshopEmailTemplate.workshop_id == workshop_id)
    templates = db.execute(stmt).scalars().all()
    return [EmailTemplateOut.model_validate(t) for t in templates]


@router.post("/email-templates", response_model=EmailTemplateOut, status_code=status.HTTP_201_CREATED)
def create_email_template(body: EmailTemplateCreate, _admin: AdminDep, db: DbDep) -> EmailTemplateOut:
    """Admin: create a new workshop email template."""
    obj = WorkshopEmailTemplate(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return EmailTemplateOut.model_validate(obj)


@router.patch("/email-templates/{template_id}", response_model=EmailTemplateOut)
def update_email_template(template_id: uuid.UUID, body: EmailTemplateUpdate, _admin: AdminDep, db: DbDep) -> EmailTemplateOut:
    """Admin: partial-update a workshop email template."""
    obj = db.get(WorkshopEmailTemplate, template_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Email template not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return EmailTemplateOut.model_validate(obj)


@router.delete("/email-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_email_template(template_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    """Admin: delete a workshop email template."""
    obj = db.get(WorkshopEmailTemplate, template_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Email template not found")
    db.delete(obj)
    db.commit()


# ── Public endpoints (literal prefix) ───────────────────────────────────────


def _get_prev_cycle_recording(workshop_id: uuid.UUID, school_id: uuid.UUID, db: DbDep) -> tuple[str | None, str | None]:
    """Return (video_embed_code, cycle_name) for the most recent past webinar
    of the given workshop that has a recording and is mapped to the given school.
    Returns (None, None) if none found."""
    row = db.execute(
        select(Webinar)
        .join(PortalMapping,
              (PortalMapping.webinar_id == Webinar.id) & (PortalMapping.school_id == school_id))
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


@router.post(
    "/public/notifications/subscribe",
    response_model=NotificationSubscriberOut,
    status_code=status.HTTP_201_CREATED,
)
def subscribe_notifications(
    body: NotificationSubscribeRequest,
    db: DbDep,
) -> NotificationSubscriberOut:
    """Public: subscribe to registration-open notifications. Idempotent on duplicate."""
    existing = db.execute(
        select(WorkshopNotificationSubscriber)
        .where(
            WorkshopNotificationSubscriber.email == body.email,
            WorkshopNotificationSubscriber.school_id == body.school_id,
            WorkshopNotificationSubscriber.cycle_name == body.cycle_name,
        )
        .options(selectinload(WorkshopNotificationSubscriber.school))
    ).scalar_one_or_none()

    if existing:
        return _subscriber_out(existing)

    obj = WorkshopNotificationSubscriber(
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
        school_id=body.school_id,
        cycle_name=body.cycle_name,
        notification_types=body.notification_types,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    obj = db.execute(
        select(WorkshopNotificationSubscriber)
        .where(WorkshopNotificationSubscriber.id == obj.id)
        .options(selectinload(WorkshopNotificationSubscriber.school))
    ).scalar_one()
    return _subscriber_out(obj)


@router.get("/public/school/{school_id}", response_model=SchoolWorkshopsResponse)
def get_school_workshops(school_id: uuid.UUID, db: DbDep) -> SchoolWorkshopsResponse:
    """Return upcoming and past workshops for a school portal (no auth)."""
    mappings = (
        db.execute(
            select(PortalMapping)
            .where(PortalMapping.school_id == school_id)
            .options(
                selectinload(PortalMapping.webinar).options(
                    selectinload(Webinar.workshop).options(
                        selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
                        selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
                    ),
                    selectinload(Webinar.cycle),
                    selectinload(Webinar.registrations),
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
        # Only show webinars from the current cycle (upcoming and past alike).
        # Webinars with no cycle assigned are excluded so stray/test webinars
        # don't leak into a school's portal list.
        cycle = webinar.cycle
        if cycle is None or not cycle.is_current:
            continue

        if is_upcoming:
            prev_embed, prev_name = _get_prev_cycle_recording(webinar.workshop_id, school_id, db)
            item = _to_item(mapping, prev_cycle_video_embed_code=prev_embed, prev_cycle_name=prev_name)
            upcoming.append(item)
        else:
            item = _to_item(mapping)
            past.append(item)

    # Sort upcoming ascending (soonest first), past descending (most recent first)
    upcoming.sort(key=lambda x: x.start_datetime or datetime.max.replace(tzinfo=timezone.utc))
    past.sort(key=lambda x: x.start_datetime or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return SchoolWorkshopsResponse(upcoming=upcoming, past=past)


@router.get("/public/school/{school_id}/webinar/by-prefix/{prefix}", response_model=WorkshopPortalItem)
def get_school_webinar_by_prefix(school_id: uuid.UUID, prefix: str, db: DbDep) -> WorkshopPortalItem:
    """Return a single webinar's portal details looked up by the first 8 hex chars of its UUID."""
    if len(prefix) != 8 or not all(c in "0123456789abcdef" for c in prefix):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="prefix must be 8 lowercase hex characters")

    mapping = (
        db.execute(
            select(PortalMapping)
            .where(
                PortalMapping.school_id == school_id,
                cast(PortalMapping.webinar_id, String).like(f"{prefix}%"),
            )
            .options(
                selectinload(PortalMapping.webinar).options(
                    selectinload(Webinar.workshop).options(
                        selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
                        selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
                    ),
                    selectinload(Webinar.cycle),
                    selectinload(Webinar.registrations),
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
        prev_embed, prev_name = _get_prev_cycle_recording(webinar.workshop_id, school_id, db)
        return _to_item(mapping, prev_cycle_video_embed_code=prev_embed, prev_cycle_name=prev_name)
    return _to_item(mapping)


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
                    selectinload(Webinar.workshop).options(
                        selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
                        selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
                    ),
                    selectinload(Webinar.cycle),
                    selectinload(Webinar.registrations),
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
        prev_embed, prev_name = _get_prev_cycle_recording(webinar.workshop_id, school_id, db)
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
        school_name: str | None = None
        if body.school_id:
            school_obj = db.get(School, body.school_id)
            school_name = school_obj.name if school_obj else None
        zoom_registrant_id = zoom_client.register_webinar(
            zoom_webinar_id=webinar.zoom_webinar_id,
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            grade=body.grade,
            school=school_name,
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


@router.post("/sync-airtable", response_model=AirtableSyncResult)
def sync_webinars_airtable(_admin: AdminDep, db: DbDep):
    """Admin: sync workshop names from Airtable, then pull webinar URLs/embed codes."""
    from src.workshops.sync import sync_all_from_airtable
    return sync_all_from_airtable(db)


@router.get("/sync-airtable/last", response_model=AirtableSyncLogOut | None)
def get_last_airtable_sync(_admin: AdminDep, db: DbDep):
    """Admin: return the most recent Airtable sync log entry."""
    return db.execute(
        select(AirtableSyncLog).order_by(AirtableSyncLog.synced_at.desc()).limit(1)
    ).scalar_one_or_none()


@router.get("/{workshop_id}", response_model=WorkshopOut)
def get_workshop(workshop_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: get workshop detail (webinars loaded separately via /webinars endpoint)."""
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
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
        objectives=[_objective_with_resources(o) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        key_action_items=list(obj.key_action_items or []),
        resources=[ContentAssetSummary.model_validate(a) for a in obj.content_assets],
    )


@router.patch("/{workshop_id}", response_model=WorkshopOut)
def update_workshop(workshop_id: uuid.UUID, body: WorkshopUpdate, _admin: AdminDep, db: DbDep):
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
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
        objectives=[_objective_with_resources(o) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        key_action_items=list(obj.key_action_items or []),
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
    impact = _delete_impact(db, [w.id for w in webinars])
    return [
        WebinarSummary(
            school_count=impact[w.id][0],
            email_send_count=impact[w.id][1],
            id=w.id,
            webinar_name=w.webinar_name,
            cohort_id=w.cohort_id,
            start_datetime=w.start_datetime,
            end_datetime=w.end_datetime,
            zoom_webinar_id=w.zoom_webinar_id,
            registration_url=w.registration_url,
            zoom_link=w.zoom_link,
            registration_count=len(w.registrations),
            slug=w.slug,
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
            selectinload(Workshop.objectives).selectinload(Objective.content_assets),
            selectinload(Workshop.content_assets).selectinload(ContentAsset.asset_type),
        )
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Workshop not found")

    fetched = db.execute(
        select(Objective)
        .where(Objective.id.in_(body.ids))
        .options(selectinload(Objective.content_assets))
    ).scalars().all() if body.ids else []

    # SQL IN does not preserve order — reorder to match the admin-defined body.ids order.
    by_id = {o.id: o for o in fetched}
    new_objectives = [by_id[oid] for oid in body.ids if oid in by_id]

    obj.objectives = list(new_objectives)
    db.flush()  # create objective_workshops join rows before stamping their sort_order

    # Persist the display order on the join table (index within body.ids)
    for order, objective in enumerate(new_objectives):
        db.execute(
            ObjectiveWorkshop.__table__.update()
            .where(
                (ObjectiveWorkshop.workshop_id == workshop_id)
                & (ObjectiveWorkshop.objective_id == objective.id)
            )
            .values(sort_order=order)
        )

    # Auto-sync workshop_resources from the union of all linked objective content assets
    all_asset_ids: dict[uuid.UUID, int] = {}
    for obj_order, objective in enumerate(new_objectives):
        for asset in sorted(objective.content_assets, key=lambda a: a.name):
            if asset.id not in all_asset_ids:
                all_asset_ids[asset.id] = len(all_asset_ids)

    db.execute(
        WorkshopResource.__table__.delete().where(
            WorkshopResource.workshop_id == workshop_id
        )
    )
    for asset_id, sort_order in all_asset_ids.items():
        db.execute(
            WorkshopResource.__table__.insert().values(
                content_asset_id=asset_id,
                workshop_id=workshop_id,
                sort_order=sort_order,
            )
        )

    db.commit()
    # Reload after commit with full selectinload chain
    obj = db.execute(
        select(Workshop)
        .where(Workshop.id == workshop_id)
        .options(
            selectinload(Workshop.webinars),
            selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
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
        objectives=[_objective_with_resources(o) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        key_action_items=list(obj.key_action_items or []),
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
        .options(selectinload(Workshop.webinars), selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type))
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
            selectinload(Workshop.objectives).selectinload(Objective.content_assets).selectinload(ContentAsset.asset_type),
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
        objectives=[_objective_with_resources(o) for o in obj.objectives],
        action_items=list(obj.action_items or []),
        key_action_items=list(obj.key_action_items or []),
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
    return _webinar_out(obj, db)
