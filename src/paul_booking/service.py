"""Booking use cases: settings, open slots, taking a request, serialising rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.app_config.models import AppConfig
from src.guest_contacts.models import GuestContact
from src.guest_contacts.parent_detection import detect_parent
from src.guest_contacts.spam_detection import detect_spam
from src.integrations.zoom import ZoomApiError
from src.integrations.zoom_paul_calendar import get_paul_busy
from src.paul_booking import emails
from src.paul_booking.availability import free_block_starts, slot_starts
from src.paul_booking.holds import (
    SlotTaken,
    active_hold_filter,
    held_block_starts,
    is_lapsed,
    reserve,
)
from src.paul_booking.models import PaulMeetingRequest, PaulTimeOff
from src.paul_booking.rules import (
    DEFAULT_NOTIFY_EMAIL,
    DEFAULT_TIMEZONE,
    HORIZON,
    MIN_NOTICE,
    as_aware,
    block_starts,
    expires_at_for,
)
from src.paul_booking.schemas import (
    ContactOut,
    RequestCreate,
    RequestOut,
    RequestReceipt,
)

logger = logging.getLogger(__name__)

# Active pending requests one email address may have open at once.
MAX_PENDING_PER_EMAIL = 2


class BookingUnavailable(Exception):
    """Booking is switched off, or Zoom cannot tell us when Paul is busy (503)."""


class InvalidSlot(Exception):
    """The requested start is not an offerable slot for that length (422)."""


class TooManyPending(Exception):
    """This email already has the maximum number of open requests (429)."""


@dataclass(frozen=True)
class BookingSettings:
    enabled: bool
    tz_name: str
    tz: ZoneInfo
    weekly_hours: dict | None
    notify_email: str


def load_settings(db: Session) -> BookingSettings:
    cfg = db.scalar(select(AppConfig))
    name = (cfg.paul_booking_timezone if cfg else None) or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        name, tz = DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)
    return BookingSettings(
        bool(cfg and cfg.paul_booking_enabled), name, tz, cfg.paul_booking_weekly_hours if cfg else None,
        (cfg.paul_booking_notify_email if cfg else None) or DEFAULT_NOTIFY_EMAIL,
    )


def require_enabled(db: Session) -> BookingSettings:
    cfg = load_settings(db)
    if not cfg.enabled:
        raise BookingUnavailable("Booking is not available")
    return cfg


def _time_off(db: Session) -> list[tuple[date, date]]:
    return [(s, e) for s, e in db.execute(select(PaulTimeOff.starts_on, PaulTimeOff.ends_on))]


def _busy(db: Session, now: datetime, *, fresh: bool):
    """Paul's Zoom busy time. Ends the session's transaction first so no pooled
    connection is held while Zoom answers (each Zoom call may take up to 15s)."""
    db.commit()
    try:
        return get_paul_busy(now + MIN_NOTICE, now + HORIZON, fresh=fresh)
    except ZoomApiError as exc:
        logger.warning("paul_booking: Zoom busy lookup failed: %s", exc)
        raise BookingUnavailable("Zoom is unavailable") from exc


def open_slots(db: Session, cfg: BookingSettings, duration: int, now: datetime) -> list[datetime]:
    """Start times offerable for a call of ``duration`` minutes right now."""
    busy = _busy(db, now, fresh=False)
    free = free_block_starts(cfg.weekly_hours, cfg.tz, _time_off(db), busy, held_block_starts(db, now), now)
    return slot_starts(free, duration)


def create_request(db: Session, cfg: BookingSettings, body: RequestCreate, now: datetime) -> RequestReceipt:
    """Take a hold and notify. Raises SlotTaken / InvalidSlot / TooManyPending / BookingUnavailable.

    A bot-flagged submission is stored as quarantined and answered with an
    identical-looking receipt: no hold, no Zoom lookup, no email.
    """
    start = as_aware(body.start)
    message = (body.message or "").strip() or f"Requested a {body.duration}-minute call with Paul."
    spam_reason = detect_spam(
        first_name=body.first_name, last_name=body.last_name, email=body.email,
        school_name=body.school_name, message=message, honeypot=body.website,
    )
    parent_reason = detect_parent(role=body.role, message=message)
    contact = GuestContact(
        first_name=body.first_name.strip(), last_name=body.last_name.strip(), email=str(body.email),
        role=body.role, school_name=body.school_name.strip(), message=message, source="paul_booking",
        is_spam=spam_reason is not None, spam_reason=spam_reason,
        is_parent=parent_reason is not None, parent_reason=parent_reason,
    )
    expires_at = expires_at_for(now, start)
    receipt = RequestReceipt(start=start, duration=body.duration, expires_at=expires_at)
    if spam_reason:
        db.add(contact)
        db.commit()
        return receipt

    wanted = block_starts(start, body.duration)
    busy = _busy(db, now, fresh=True)  # re-read from Zoom rather than the cache, before any DB work
    if set(wanted) & set(held_block_starts(db, now)):
        raise SlotTaken
    # Rules and Zoom only (holds were checked above).
    rules_free = free_block_starts(cfg.weekly_hours, cfg.tz, _time_off(db), busy, [], now)
    if start not in slot_starts(rules_free, body.duration):
        raise InvalidSlot
    open_count = db.scalar(
        select(func.count()).select_from(PaulMeetingRequest).join(GuestContact)
        .where(func.lower(GuestContact.email) == str(body.email).lower(), PaulMeetingRequest.status == "pending",
               active_hold_filter(now))
    )
    if (open_count or 0) >= MAX_PENDING_PER_EMAIL:
        raise TooManyPending

    db.add(contact)
    db.flush()
    request = PaulMeetingRequest(
        guest_contact_id=contact.id, start_at=start, duration_minutes=body.duration,
        visitor_timezone=body.time_zone, status="pending", expires_at=expires_at, created_at=now,
    )
    reserve(db, request, wanted, now)  # SlotTaken on a lost race; session rolled back
    db.commit()
    emails.notify_new_request(db, request, cfg)
    return receipt


def request_out(req: PaulMeetingRequest, now: datetime) -> RequestOut:
    c = req.contact
    return RequestOut(
        id=req.id, start_at=as_aware(req.start_at), duration_minutes=req.duration_minutes,
        visitor_timezone=req.visitor_timezone, status=req.status, lapsed=is_lapsed(req, now),
        expires_at=as_aware(req.expires_at), created_at=as_aware(req.created_at),
        decided_at=as_aware(req.decided_at) if req.decided_at else None,
        decline_note=req.decline_note, zoom_join_url=req.zoom_join_url,
        contact=ContactOut(
            first_name=c.first_name, last_name=c.last_name, email=c.email, school_name=c.school_name,
            role=c.role, message=c.message, is_parent=c.is_parent,
        ),
    )


def overlapping_requests(db: Session, starts_on: date, ends_on: date, cfg: BookingSettings, now: datetime) -> list[PaulMeetingRequest]:
    """Active (pending-not-lapsed or approved) requests starting on those days in Paul's zone."""
    lo = datetime.combine(starts_on, datetime.min.time(), tzinfo=cfg.tz).astimezone(timezone.utc)
    hi = datetime.combine(ends_on + timedelta(days=1), datetime.min.time(), tzinfo=cfg.tz).astimezone(timezone.utc)
    rows = db.scalars(
        select(PaulMeetingRequest)
        .where(PaulMeetingRequest.start_at < hi, PaulMeetingRequest.start_at >= lo,
               PaulMeetingRequest.status.in_(("pending", "approving", "approved")))
        .order_by(PaulMeetingRequest.start_at)
    ).unique()
    return [r for r in rows if not is_lapsed(r, now)]
