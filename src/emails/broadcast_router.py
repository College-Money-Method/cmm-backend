"""Broadcast (one-off admin email) endpoints — super_admin ONLY.

NOTE: All static paths (/audience-preview) must be declared BEFORE
parameterized paths (/{broadcast_id}) — Starlette matches in order (same
convention as ``communications/router.py``).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.emails.audience import resolve_audience
from src.emails.broadcast_models import Broadcast
from src.emails.analytics import engagement_for_broadcast
from src.emails.broadcast_schemas import (
    AudiencePreviewOut,
    BroadcastCreate,
    BroadcastDetailOut,
    BroadcastOut,
    EmailEngagementOut,
    RecipientStatusRow,
    SendTestResultOut,
)
from src.emails.broadcast_send import send_broadcast_batch, send_test
from src.emails.models import EmailSendLog
from src.schools.models import Contact

router = APIRouter(prefix="/api/v1/emails/broadcasts", tags=["emails"])


def _broadcast_out(broadcast: Broadcast) -> BroadcastOut:
    return BroadcastOut(
        id=broadcast.id,
        subject=broadcast.subject,
        body_json=json.loads(broadcast.body_json),
        school_scope=broadcast.school_scope,
        role_filter=broadcast.role_filter,
        opt_in_filter=broadcast.opt_in_filter,
        created_by=broadcast.created_by,
        created_at=broadcast.created_at,
        status=broadcast.status,
    )


def _get_broadcast_or_404(db: Session, broadcast_id: uuid.UUID) -> Broadcast:
    broadcast = db.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broadcast not found")
    return broadcast


@router.get("/audience-preview", response_model=AudiencePreviewOut)
def preview_audience(
    _admin: AdminDep,
    db: DbDep,
    school_scope: str = Query(...),
    role_filter: str = Query("all"),
    opt_in_filter: str = Query("opted_in"),
) -> AudiencePreviewOut:
    """Live matched-count preview for the compose UI's audience selector.

    Always reports how many of the matched contacts are NOT opted in
    (``auto_emails is False``), even when ``opt_in_filter="opted_in"`` already
    excludes them, so the UI can warn the admin BEFORE they switch the filter
    to "all" and reach those contacts.
    """
    matched = resolve_audience(db, school_scope, role_filter, opt_in_filter)
    non_opted_in = sum(1 for c in matched if not c.auto_emails)
    return AudiencePreviewOut(
        matched_count=len(matched),
        non_opted_in_count=non_opted_in,
        warning=opt_in_filter == "all" and non_opted_in > 0,
    )


@router.post("", response_model=BroadcastOut, status_code=status.HTTP_201_CREATED)
def create_broadcast(payload: BroadcastCreate, admin: AdminDep, db: DbDep) -> BroadcastOut:
    broadcast = Broadcast(
        subject=payload.subject,
        body_json=json.dumps(payload.body_json),
        school_scope=payload.school_scope,
        role_filter=payload.role_filter,
        opt_in_filter=payload.opt_in_filter,
        created_by=admin.user_id,
        status="draft",
    )
    db.add(broadcast)
    db.commit()
    db.refresh(broadcast)
    return _broadcast_out(broadcast)


@router.get("", response_model=list[BroadcastOut])
def list_broadcasts(_admin: AdminDep, db: DbDep) -> list[BroadcastOut]:
    broadcasts = db.scalars(select(Broadcast).order_by(Broadcast.created_at.desc())).all()
    return [_broadcast_out(b) for b in broadcasts]


@router.get("/{broadcast_id}", response_model=BroadcastDetailOut)
def get_broadcast(broadcast_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> BroadcastDetailOut:
    broadcast = _get_broadcast_or_404(db, broadcast_id)

    counts_stmt = (
        select(EmailSendLog.status, func.count())
        .where(EmailSendLog.broadcast_id == broadcast_id)
        .group_by(EmailSendLog.status)
    )
    counts = dict(db.execute(counts_stmt).all())

    rows = db.scalars(
        select(EmailSendLog)
        .where(EmailSendLog.broadcast_id == broadcast_id)
        .order_by(EmailSendLog.sent_at.desc())
    ).all()

    base = _broadcast_out(broadcast)
    return BroadcastDetailOut(
        **base.model_dump(),
        sent_count=counts.get("sent", 0),
        dry_run_count=counts.get("dry_run", 0),
        suppressed_count=counts.get("suppressed", 0),
        failed_count=counts.get("failed", 0),
        recipients=[
            RecipientStatusRow(recipient_email=r.recipient_email, status=r.status, sent_at=r.sent_at)
            for r in rows
        ],
    )


@router.get("/{broadcast_id}/analytics", response_model=EmailEngagementOut)
def get_broadcast_analytics(broadcast_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> EmailEngagementOut:
    """Open/click engagement for one broadcast. Opens are inflated by Apple Mail
    Privacy Protection (see ``EmailEngagementOut``)."""
    _get_broadcast_or_404(db, broadcast_id)
    e = engagement_for_broadcast(db, broadcast_id)
    return EmailEngagementOut(
        sent_count=e.sent_count,
        unique_opened=e.unique_opened,
        unique_clicked=e.unique_clicked,
        open_rate=e.open_rate,
        click_rate=e.click_rate,
    )


@router.post("/{broadcast_id}/send", status_code=status.HTTP_202_ACCEPTED)
def send_broadcast(
    broadcast_id: uuid.UUID, _admin: AdminDep, db: DbDep, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Resolve the audience now (snapshot), flip status to "sending", and hand
    the recipient list off to a background task so the request returns
    immediately — no queue infra per YAGNI, matches the existing
    ``submissions_router`` background-task convention."""
    broadcast = _get_broadcast_or_404(db, broadcast_id)

    # Atomically claim the send: flip draft->sending in one guarded UPDATE and
    # check rowcount. A plain read-check-then-write lets two near-simultaneous
    # POSTs (double-click / client retry) both pass the status check and each
    # queue a full send. Only the request that actually transitions the row
    # proceeds; the loser gets 409.
    claimed = db.execute(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id, Broadcast.status == "draft")
        .values(status="sending")
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Broadcast already {broadcast.status}, cannot send again",
        )
    db.commit()

    contacts = resolve_audience(db, broadcast.school_scope, broadcast.role_filter, broadcast.opt_in_filter)
    contact_ids = [c.id for c in contacts]

    background_tasks.add_task(send_broadcast_batch, broadcast.id, contact_ids)
    return {"status": "sending", "recipient_count": str(len(contact_ids))}


@router.post("/{broadcast_id}/send-test", response_model=SendTestResultOut)
def send_test_broadcast(broadcast_id: uuid.UUID, admin: AdminDep, db: DbDep) -> SendTestResultOut:
    """Send an immediate (synchronous, non-background) test copy to the
    requesting admin's own contact email so they can preview the rendered
    result before committing to the full send."""
    broadcast = _get_broadcast_or_404(db, broadcast_id)

    # Preferred path: the admin has their own Contact row — send to their own
    # email with their own merge-tag context.
    contact = db.scalar(select(Contact).where(Contact.user_id == admin.user_id))
    if contact is not None and contact.email:
        send_test(db, broadcast, contact)
        return SendTestResultOut(sent_to=contact.email, used_sample_contact=False)

    # Fallback: super_admins are not Contacts. Send to the admin's authenticated
    # login email, borrowing a sample audience contact for merge-tag context so
    # the preview renders realistically.
    if not admin.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No email address on file for the current admin — cannot send a test",
        )
    audience = resolve_audience(
        db, broadcast.school_scope, broadcast.role_filter, broadcast.opt_in_filter
    )
    sample = audience[0] if audience else None
    send_test(db, broadcast, sample, override_to=admin.email)
    return SendTestResultOut(sent_to=admin.email, used_sample_contact=sample is not None)
