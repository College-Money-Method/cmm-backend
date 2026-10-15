"""Held slots: which requests currently occupy time, and taking a hold safely."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.paul_booking.models import PaulMeetingRequest, PaulMeetingRequestBlock
from src.paul_booking.rules import as_aware


class SlotTaken(Exception):
    """Another active request already holds one of the requested blocks."""


def is_lapsed(request: PaulMeetingRequest, now: datetime) -> bool:
    return request.status == "pending" and as_aware(request.expires_at) <= now


def active_hold_filter(now: datetime):
    """SQL predicate: approved, being approved, or pending and not yet lapsed."""
    return or_(
        PaulMeetingRequest.status.in_(("approved", "approving")),
        and_(PaulMeetingRequest.status == "pending", PaulMeetingRequest.expires_at > now),
    )


def held_block_starts(db: Session, now: datetime) -> list[datetime]:
    """Start of every 30-minute block an active request holds."""
    rows = db.scalars(
        select(PaulMeetingRequestBlock.block_start)
        .join(PaulMeetingRequest, PaulMeetingRequest.id == PaulMeetingRequestBlock.request_id)
        .where(active_hold_filter(now))
    )
    return [as_aware(r) for r in rows]


def reserve(db: Session, request: PaulMeetingRequest, starts: list[datetime], now: datetime) -> None:
    """Insert the request's blocks, reclaiming lapsed holds at those starts first.

    Runs in the caller's transaction (the request row must already be added).
    The UNIQUE ``block_start`` decides any race; the loser gets ``SlotTaken`` and
    the session is rolled back, so nothing from this attempt is kept.
    """
    lapsed = select(PaulMeetingRequest.id).where(
        PaulMeetingRequest.status == "pending", PaulMeetingRequest.expires_at <= now
    )
    db.execute(
        delete(PaulMeetingRequestBlock)
        .where(PaulMeetingRequestBlock.block_start.in_(starts))
        .where(PaulMeetingRequestBlock.request_id.in_(lapsed))
        .execution_options(synchronize_session=False)
    )
    db.add(request)
    db.flush()
    db.add_all(PaulMeetingRequestBlock(request_id=request.id, block_start=s) for s in starts)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise SlotTaken from exc


def release(db: Session, request_id) -> None:
    """Free every block a request holds (decline)."""
    db.execute(
        delete(PaulMeetingRequestBlock)
        .where(PaulMeetingRequestBlock.request_id == request_id)
        .execution_options(synchronize_session=False)
    )
