"""Admin (super_admin) endpoints: requests calendar, approve/decline, days off."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.paul_booking import decisions, service
from src.paul_booking.models import PaulMeetingRequest, PaulTimeOff
from src.paul_booking.public_router import decision_error_response
from src.paul_booking.rules import as_aware
from src.paul_booking.schemas import (
    DeclineIn,
    RequestOut,
    TimeOffCreate,
    TimeOffCreated,
    TimeOffOut,
)

router = APIRouter(prefix="/api/v1/admin/paul-booking", tags=["paul-booking-admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/requests", response_model=list[RequestOut])
def list_requests(
    db: DbDep,
    _admin: AdminDep,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
):
    """Every request starting in [from, to), any status, soonest first, ``lapsed`` computed."""
    now = _now()
    rows = db.scalars(
        select(PaulMeetingRequest)
        .where(PaulMeetingRequest.start_at >= as_aware(from_), PaulMeetingRequest.start_at < as_aware(to))
        .order_by(PaulMeetingRequest.start_at)
    ).unique()
    return [service.request_out(r, now) for r in rows]


@router.post("/requests/{request_id}/approve", response_model=RequestOut)
def approve_request(request_id: uuid.UUID, db: DbDep, _admin: AdminDep):
    now = _now()
    try:
        return service.request_out(decisions.approve(db, request_id, now), now)
    except decisions.DecisionError as exc:
        return decision_error_response(exc)


@router.post("/requests/{request_id}/decline", response_model=RequestOut)
def decline_request(request_id: uuid.UUID, db: DbDep, _admin: AdminDep, body: DeclineIn | None = None):
    now = _now()
    try:
        return service.request_out(decisions.decline(db, request_id, body.note if body else None, now), now)
    except decisions.DecisionError as exc:
        return decision_error_response(exc)


@router.get("/time-off", response_model=list[TimeOffOut])
def list_time_off(db: DbDep, _admin: AdminDep, from_: date = Query(alias="from"), to: date = Query()):
    """Days off overlapping [from, to] (inclusive dates)."""
    return db.scalars(
        select(PaulTimeOff)
        .where(PaulTimeOff.ends_on >= from_, PaulTimeOff.starts_on <= to)
        .order_by(PaulTimeOff.starts_on)
    ).all()


@router.post("/time-off", response_model=TimeOffCreated, status_code=status.HTTP_201_CREATED)
def add_time_off(body: TimeOffCreate, db: DbDep, _admin: AdminDep):
    """Add days off. Never declines anything: it reports the calls it overlaps."""
    item = PaulTimeOff(starts_on=body.starts_on, ends_on=body.ends_on, note=(body.note or "").strip() or None)
    db.add(item)
    db.commit()
    db.refresh(item)
    now = _now()
    overlapping = service.overlapping_requests(db, body.starts_on, body.ends_on, service.load_settings(db), now)
    return TimeOffCreated(item=TimeOffOut.model_validate(item), overlapping_requests=[service.request_out(r, now) for r in overlapping])


@router.delete("/time-off/{time_off_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_time_off(time_off_id: uuid.UUID, db: DbDep, _admin: AdminDep):
    item = db.get(PaulTimeOff, time_off_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Time off not found")
    db.delete(item)
    db.commit()
