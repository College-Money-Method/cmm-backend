"""Approve / decline a call request. One service behind both entry points
(Paul's email link and the admin calendar).

No background job: the state machine is a conditional UPDATE per transition.
``approving`` is a short claim around the Zoom call; a claim older than
``APPROVE_CLAIM_TTL`` belongs to a crashed attempt and may be taken over.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from src.guest_contacts.models import GuestContact
from src.integrations.zoom import ZoomApiError
from src.integrations.zoom_paul_meetings import create_paul_meeting
from src.paul_booking import emails, service
from src.paul_booking.holds import release
from src.paul_booking.models import PaulMeetingRequest
from src.paul_booking.rules import APPROVE_CLAIM_TTL

logger = logging.getLogger(__name__)


class DecisionError(Exception):
    """A decision that cannot proceed; the router maps ``status_code`` straight to HTTP."""

    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _load(db: Session, request_id: uuid.UUID) -> PaulMeetingRequest | None:
    db.expire_all()
    return db.get(PaulMeetingRequest, request_id)


def _why_not(db: Session, request_id: uuid.UUID, now: datetime) -> DecisionError:
    """Explain a claim that matched no row: 404, 410 lapsed, or 409 with the current status."""
    req = _load(db, request_id)
    if req is None:
        return DecisionError(404, "Request not found")
    if req.status in ("approved", "declined", "approving"):
        return DecisionError(409, {"status": req.status})
    return DecisionError(410, "This request has lapsed")


def _stale_claim(now: datetime):
    """SQL predicate: an ``approving`` claim old enough to belong to a crashed attempt."""
    return and_(PaulMeetingRequest.status == "approving", PaulMeetingRequest.claimed_at < now - APPROVE_CLAIM_TTL)


def _our_claim(request_id: uuid.UUID, claimed_at: datetime):
    """SQL predicate: the request is still ``approving`` under the claim taken at ``claimed_at``,
    so a slow attempt cannot overwrite a takeover or a decline that happened meanwhile."""
    return and_(
        PaulMeetingRequest.id == request_id,
        PaulMeetingRequest.status == "approving",
        PaulMeetingRequest.claimed_at == claimed_at,
    )


def _resolve_contact(db: Session, contact_id: uuid.UUID, now: datetime) -> None:
    db.execute(
        update(GuestContact)
        .where(GuestContact.id == contact_id, GuestContact.resolved_at.is_(None))
        .values(resolved_at=now)
    )


def approve(db: Session, request_id: uuid.UUID, now: datetime) -> PaulMeetingRequest:
    """Claim, create the Zoom meeting, mark approved, email both sides.

    Raises ``DecisionError``: 404 unknown, 409 already decided or being approved,
    410 lapsed, 502 Zoom failed (the request is back to pending, hold kept).
    """
    claimed = db.execute(
        update(PaulMeetingRequest)
        .where(
            PaulMeetingRequest.id == request_id,
            or_(
                and_(PaulMeetingRequest.status == "pending", PaulMeetingRequest.expires_at > now),
                # A crashed attempt still holds the slot past its reply-by time,
                # so it stays approvable until the call would have started.
                and_(_stale_claim(now), PaulMeetingRequest.start_at > now),
            ),
        )
        .values(status="approving", claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claimed.rowcount == 0:
        raise _why_not(db, request_id, now)

    req = _load(db, request_id)
    cfg = service.load_settings(db)
    meeting = dict(
        topic=f"College Money Method x {req.contact.school_name or req.contact.first_name}",
        agenda=req.contact.message,
        start_at=req.start_at,
        duration_minutes=req.duration_minutes,
        timezone_name=cfg.tz_name,
    )
    guest_contact_id = req.guest_contact_id
    db.commit()  # hand the pooled connection back while Zoom answers
    try:
        meeting_id, join_url = create_paul_meeting(**meeting)
    except Exception as exc:
        # Whatever went wrong, hand the request back so Paul can retry; the hold stays.
        db.rollback()
        db.execute(
            update(PaulMeetingRequest)
            .where(_our_claim(request_id, now))
            .values(status="pending", claimed_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if isinstance(exc, ZoomApiError):
            logger.warning("paul_booking approve: Zoom failed request=%s %s", request_id, exc)
            raise DecisionError(502, f"Zoom could not create the meeting: {exc}") from exc
        raise

    finished = db.execute(
        update(PaulMeetingRequest)
        .where(_our_claim(request_id, now))
        .values(status="approved", zoom_meeting_id=meeting_id, zoom_join_url=join_url, decided_at=now, claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    if finished.rowcount == 0:
        # Our claim went stale and someone else took over or declined meanwhile;
        # their outcome stands. The meeting we just made is reported, not hidden.
        db.rollback()
        logger.error("paul_booking approve: claim lost after Zoom create request=%s meeting=%s", request_id, meeting_id)
        raise _why_not(db, request_id, now)
    _resolve_contact(db, guest_contact_id, now)
    db.commit()

    req = _load(db, request_id)
    emails.notify_approved(db, req, cfg.tz, now)
    return _load(db, request_id)


def decline(db: Session, request_id: uuid.UUID, note: str | None, now: datetime) -> PaulMeetingRequest:
    """Release the hold, record the note, email the visitor.

    A stale ``approving`` claim can be declined too, whatever the time.
    Raises ``DecisionError``: 404 unknown, 409 already decided or being approved, 410 lapsed.
    """
    cleaned = (note or "").strip() or None
    declined = db.execute(
        update(PaulMeetingRequest)
        .where(
            PaulMeetingRequest.id == request_id,
            or_(
                and_(PaulMeetingRequest.status == "pending", PaulMeetingRequest.expires_at > now),
                _stale_claim(now),  # a crashed approval must not hold the slot forever
            ),
        )
        .values(status="declined", decline_note=cleaned, decided_at=now, claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    if declined.rowcount == 0:
        db.rollback()
        raise _why_not(db, request_id, now)
    release(db, request_id)
    req = _load(db, request_id)
    _resolve_contact(db, req.guest_contact_id, now)
    db.commit()

    req = _load(db, request_id)
    emails.notify_declined(db, req)
    return _load(db, request_id)
