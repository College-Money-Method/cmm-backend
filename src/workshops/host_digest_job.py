"""Hourly (08:00-20:00 Pacific) host digest: emails each upcoming webinar's Zoom
co-hosts, plus the configured extra recipients, a snapshot of approved
registrants' questions and the link to the live questions page.

Exactly-once per (webinar, recipient, start time), by claim-before-send: a row in
``webinar_host_digest_deliveries`` is inserted ON CONFLICT DO NOTHING before the
send, so a second tick, a second worker or an overlapping deploy finds the claim
taken and skips that recipient. A failed send deletes the claim so the next tick
retries only that recipient. Because the start time is part of the key, a
rescheduled webinar earns a fresh email. Later ticks also pick up webinars
created late and cover a restart that missed 08:00 (the scheduler is in-memory).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, selectinload

from src.config import settings
from src.db.base import get_session_factory
from src.emails.ses_client import send_email
from src.integrations.zoom_webinar_hosts import ZoomWebinarNotFound, get_webinar_cohost_emails
from src.schools.display_timezone import resolve_display_timezone
from src.utils import single_writer
from src.workshops.host_access import (
    approved_registrations,
    ensure_host_access_token,
    host_link_expires_at,
    host_questions_url,
)
from src.workshops.host_digest_email import render_host_digest
from src.workshops.models import Webinar, WebinarHostDigestDelivery

logger = logging.getLogger(__name__)

_SOURCE = "webinar_host_digest"
_LOCK = "webinar-host-digest"
_JOB_ID = "webinar_host_digest_hourly"
_TIMEZONE = ZoneInfo("America/Los_Angeles")
# If Zoom is unreachable and the session is further away than this, wait for a
# later tick instead of sending Paul a "please forward" fallback.
_DEFER_ZOOM_FAILURE_BEYOND = timedelta(hours=12)
# A claim still "claimed" after this long means the worker died mid-send (e.g. a
# deploy SIGTERM); it is taken over so the recipient isn't silently dropped.
_STALE_CLAIM_AFTER = timedelta(minutes=15)


def register_host_digest_job(scheduler) -> None:
    """Hourly 08:00-20:00 Pacific. Idempotent — fixed id, replace_existing."""
    scheduler.add_job(
        run_host_digest,
        "cron",
        hour="8-20",
        minute=0,
        timezone=_TIMEZONE,
        id=_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info("Host digest job registered — hourly 08:00-20:00 America/Los_Angeles")


def _as_aware(value: datetime) -> datetime:
    # SQLite (tests) hands timestamps back naive; Postgres returns aware UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _end_of_tomorrow(now: datetime) -> datetime:
    local = now.astimezone(_TIMEZONE)
    day_after = datetime.combine(local.date() + timedelta(days=2), time.min, tzinfo=_TIMEZONE)
    return day_after.astimezone(timezone.utc)


def _select_due(db: Session, now: datetime) -> list[Webinar]:
    """Zoom-backed webinars starting after now and before the end of tomorrow (LA).

    ``start > now`` (not ``end > now``) so a session already in progress is never
    swept in, including on the first enable.
    """
    return list(
        db.scalars(
            select(Webinar)
            .where(
                Webinar.zoom_webinar_id.is_not(None),
                Webinar.start_datetime.is_not(None),
                Webinar.start_datetime > now,
                Webinar.start_datetime < _end_of_tomorrow(now),
            )
            .options(selectinload(Webinar.workshop))
            .order_by(Webinar.start_datetime)
        )
    )


def _claim_recipient(db: Session, webinar: Webinar, email: str) -> bool:
    """INSERT the claim; True only if this call won it. Dialect insert is picked
    at runtime because prod is Postgres and the tests run on SQLite."""
    dialect = postgresql if db.get_bind().dialect.name == "postgresql" else sqlite
    stmt = (
        dialect.insert(WebinarHostDigestDelivery)
        .values(
            webinar_id=webinar.id,
            recipient_email=email,
            start_datetime=webinar.start_datetime,
            status="claimed",
        )
        .on_conflict_do_nothing()
        .returning(WebinarHostDigestDelivery.id)
    )
    claim_id = db.execute(stmt).scalar_one_or_none()
    if claim_id is None:
        claim_id = _take_over_stale_claim(db, webinar, email)
    db.commit()
    return claim_id is not None


def _take_over_stale_claim(db: Session, webinar: Webinar, email: str):
    """Conditional UPDATE so only one concurrent tick can take over an abandoned claim."""
    now = datetime.now(timezone.utc)
    return db.execute(
        update(WebinarHostDigestDelivery)
        .where(
            WebinarHostDigestDelivery.webinar_id == webinar.id,
            WebinarHostDigestDelivery.recipient_email == email,
            WebinarHostDigestDelivery.start_datetime == webinar.start_datetime,
            WebinarHostDigestDelivery.status == "claimed",
            WebinarHostDigestDelivery.updated_at < now - _STALE_CLAIM_AFTER,
        )
        .values(updated_at=now)
        .returning(WebinarHostDigestDelivery.id)
    ).scalar_one_or_none()


def _set_status(db: Session, webinar: Webinar, email: str, status: str) -> None:
    key = (
        WebinarHostDigestDelivery.webinar_id == webinar.id,
        WebinarHostDigestDelivery.recipient_email == email,
        WebinarHostDigestDelivery.start_datetime == webinar.start_datetime,
    )
    if status == "failed":
        db.execute(delete(WebinarHostDigestDelivery).where(*key))  # release: next tick retries
    else:
        db.execute(update(WebinarHostDigestDelivery).where(*key).values(status=status))
    db.commit()


def _recipients(cohosts: list[str] | None) -> tuple[list[str], list[str]]:
    """(co-hosts, extras), each lowercased; extras that are also co-hosts are dropped."""
    cohost_list = list(dict.fromkeys(e.strip().lower() for e in (cohosts or []) if e.strip()))
    extras = [e.strip().lower() for e in settings.webinar_host_digest_extra_recipients if e.strip()]
    extra_list = [e for e in dict.fromkeys(extras) if e not in cohost_list]
    return cohost_list, extra_list


def _send_one(db: Session, webinar: Webinar, email: str, subject: str, html: str, text: str) -> str | None:
    """Claim, send, record. Returns the send status, or None if already claimed."""
    if not _claim_recipient(db, webinar, email):
        return None
    try:
        status = send_email(
            db, to=email, subject=subject, html=html, text=text, source=_SOURCE, webinar_id=webinar.id
        ).status
    except Exception:
        logger.exception("Host digest: send to a recipient of webinar %s failed", webinar.id)
        db.rollback()
        status = "failed"
    _set_status(db, webinar, email, status)
    if status in ("sandboxed", "suppressed", "dry_run"):
        logger.warning("Host digest: webinar %s recipient not delivered (%s)", webinar.id, status)
    return status


def _deliver(db: Session, webinar: Webinar, now: datetime, tz: ZoneInfo) -> int:
    # Zoom is called BEFORE claiming so a lookup failure never strands a claim.
    try:
        cohosts = get_webinar_cohost_emails(webinar.zoom_webinar_id)
    except ZoomWebinarNotFound:
        logger.warning("Host digest: webinar %s no longer exists in Zoom — skipping", webinar.id)
        return 0
    lookup_failed = cohosts is None
    start = _as_aware(webinar.start_datetime)
    if lookup_failed and start - now > _DEFER_ZOOM_FAILURE_BEYOND:
        logger.info("Host digest: Zoom co-host lookup failed for webinar %s — deferring", webinar.id)
        return 0

    cohost_list, extra_list = _recipients(cohosts)
    expires_at = host_link_expires_at(webinar)
    if expires_at is None:  # unreachable given selection; guards the renderer
        return 0
    link = host_questions_url(ensure_host_access_token(db, webinar))
    regs = approved_registrations(db, webinar.id)
    logger.info(
        "Host digest: webinar %s — %d co-host(s), %d extra recipient(s), %d external domain(s)",
        webinar.id,
        len(cohost_list),
        len(extra_list),
        len({e.rsplit("@", 1)[-1] for e in cohost_list}),
    )

    sent = 0
    undelivered: list[tuple[str, str]] = []
    if cohost_list:
        subject, html, text = render_host_digest(webinar, regs, link, expires_at, tz=tz)
        for email in cohost_list:
            status = _send_one(db, webinar, email, subject, html, text)
            if status == "sent":
                sent += 1
            elif status is not None:
                undelivered.append((email, status))

    if extra_list:
        subject, html, text = render_host_digest(
            webinar, regs, link, expires_at, cohost_lookup_failed=lookup_failed, undelivered=undelivered, tz=tz
        )
        for email in extra_list:
            if _send_one(db, webinar, email, subject, html, text) == "sent":
                sent += 1
    return sent


def _run(db: Session, now: datetime) -> int:
    if not settings.webinar_host_digest_enabled:
        return 0
    tz = resolve_display_timezone()
    sent = 0
    for webinar in _select_due(db, now):
        try:
            sent += _deliver(db, webinar, now, tz)
        except Exception:
            logger.exception("Host digest failed for webinar %s — skipping", webinar.id)
            db.rollback()
    if sent:
        logger.info("Host digest sent %d email(s)", sent)
    return sent


def run_host_digest(db: Session | None = None, now: datetime | None = None) -> int:
    """Scheduler entry point (and direct test / manual invocation). Returns emails sent.

    Tests and manual runs pass their own session and clock. Only the sessionless
    scheduler path takes the leader lock — prod runs two uvicorn workers, each
    with its own scheduler.
    """
    now = _as_aware(now) if now else datetime.now(timezone.utc)
    if db is not None:
        return _run(db, now)

    if not single_writer.is_leader(_LOCK):
        logger.info("Host digest skipped: this worker is not the scheduler leader")
        return 0
    session = get_session_factory()()
    try:
        return _run(session, now)
    except Exception:
        logger.exception("Host digest run failed")
        return 0
    finally:
        session.close()
