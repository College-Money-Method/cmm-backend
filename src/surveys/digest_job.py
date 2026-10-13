"""Weekday-morning survey digest: one email per super_admin listing actionable
responses that have not been in a digest yet.

Why a digest and not an email per response: feedback-widget responses are
high-volume micro-feedback, and most are plain positive ratings. Only
actionable ones (see ``src.surveys.actionable``) are listed, and nothing is
sent on a day with none.

Exactly-once per response, by claim-before-send: the run stamps
``digest_sent_at`` on the rows it is about to email (only where it is still
NULL) and commits before sending. A second worker or a re-run therefore finds
nothing to claim. If no admin email could be delivered, the claim is released
so the next run retries. Selection is by ``digest_sent_at IS NULL``, never by a
time window, so a run missed during a deploy is caught up by the next one — and
Monday's run naturally picks up the weekend.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.db.base import get_session_factory
from src.emails.school_links import email_origin
from src.emails.ses_client import send_email
from src.schools.models import School
from src.surveys.actionable import needs_review_clause
from src.surveys.digest_email import DIGEST_TIMEZONE, DigestStats, render_survey_digest
from src.surveys.models import SurveyResponse
from src.utils import single_writer

logger = logging.getLogger(__name__)

_SOURCE = "survey_digest"
_LOCK = "survey-digest"
_JOB_ID = "survey_digest_weekday_morning"
# A send that reached SES, or was intentionally withheld (sandbox/suppressed),
# counts as handled. Only "failed" means the admin did not get it.
_HANDLED_STATUSES = {"sent", "sandboxed", "suppressed", "dry_run"}

EmailResolver = Callable[[list[uuid.UUID]], list[str]]


def register_survey_digest_job(scheduler) -> None:
    """Weekdays 08:00 Pacific. Idempotent — fixed id, replace_existing."""
    scheduler.add_job(
        run_survey_digest,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=0,
        timezone=DIGEST_TIMEZONE,
        id=_JOB_ID,
        replace_existing=True,
        # A restart a few minutes around 08:00 should still send today's digest.
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("Survey digest job registered — weekdays 08:00 America/Los_Angeles")


def _supabase_email_resolver(user_ids: list[uuid.UUID]) -> list[str]:
    """Look up each admin's login email. Admins are few, so one call each is fine."""
    from src.db.client import get_supabase  # lazy: no Supabase client at import time

    supabase = get_supabase()
    emails: list[str] = []
    for user_id in user_ids:
        try:
            resp = supabase.auth.admin.get_user_by_id(str(user_id))
            email = resp.user.email if resp and resp.user else None
        except Exception:
            logger.exception("Survey digest: could not resolve email for admin %s", user_id)
            continue
        if email:
            emails.append(email)
    return emails


def _admin_emails(db: Session, resolve: EmailResolver) -> list[str]:
    user_ids = list(db.scalars(select(UserRole.user_id).where(UserRole.role == "super_admin").distinct()))
    return sorted(set(resolve(user_ids))) if user_ids else []


def _claim(db: Session, now: datetime) -> list[SurveyResponse]:
    """Stamp every unsent, still-unreviewed actionable response and return the
    rows this run owns. One an admin already handled in the dashboard is not
    news, so it is left out of the email."""
    candidate_ids = list(
        db.scalars(
            select(SurveyResponse.id).where(SurveyResponse.digest_sent_at.is_(None), needs_review_clause())
        )
    )
    if not candidate_ids:
        return []
    claimed_ids = list(
        db.scalars(
            update(SurveyResponse)
            .where(SurveyResponse.id.in_(candidate_ids), SurveyResponse.digest_sent_at.is_(None))
            .values(digest_sent_at=now)
            .returning(SurveyResponse.id)
        )
    )
    db.commit()
    if not claimed_ids:
        return []
    return list(db.scalars(select(SurveyResponse).where(SurveyResponse.id.in_(claimed_ids))))


def _release(db: Session, rows: list[SurveyResponse]) -> None:
    db.execute(
        update(SurveyResponse)
        .where(SurveyResponse.id.in_([r.id for r in rows]))
        .values(digest_sent_at=None)
    )
    db.commit()


def _stats(db: Session, now: datetime) -> DigestStats:
    """Totals for the period since the previous weekday run (Monday covers the weekend)."""
    is_monday = now.astimezone(DIGEST_TIMEZONE).weekday() == 0
    since = now - timedelta(days=3 if is_monday else 1)
    total, up, down, avg = db.execute(
        select(
            func.count(),
            func.count().filter(SurveyResponse.rating_thumbs.is_(True)),
            func.count().filter(SurveyResponse.rating_thumbs.is_(False)),
            func.avg(SurveyResponse.rating_stars),
        ).where(SurveyResponse.created_at >= since)
    ).one()
    return DigestStats(
        period_label="since Friday" if is_monday else "in the last 24 hours",
        total=total or 0,
        thumbs_up=up or 0,
        thumbs_down=down or 0,
        avg_stars=float(avg) if avg is not None else None,
    )


def _school_names(db: Session, rows: list[SurveyResponse]) -> dict[str, str]:
    """``survey_responses.school_id`` is free text from the widget; ignore non-UUIDs."""
    ids: set[uuid.UUID] = set()
    for row in rows:
        if not row.school_id:
            continue
        try:
            ids.add(uuid.UUID(row.school_id))
        except ValueError:
            continue
    if not ids:
        return {}
    return {str(sid): name for sid, name in db.execute(select(School.id, School.name).where(School.id.in_(ids)))}


def _send_digest(db: Session, resolve: EmailResolver, now: datetime) -> int:
    rows = _claim(db, now)
    if not rows:
        logger.info("Survey digest: nothing actionable since the last digest — no email")
        return 0
    try:
        return _deliver(db, rows, resolve, now)
    except Exception:
        # Anything unexpected after the claim must not strand the rows as "sent".
        db.rollback()
        _release(db, rows)
        raise


def _deliver(db: Session, rows: list[SurveyResponse], resolve: EmailResolver, now: datetime) -> int:
    recipients = _admin_emails(db, resolve)
    if not recipients:
        logger.error("Survey digest: no super_admin email could be resolved — releasing %d rows", len(rows))
        _release(db, rows)
        return 0

    subject, html, text = render_survey_digest(rows, _stats(db, now), _school_names(db, rows), email_origin(), now)

    delivered = 0
    for to in recipients:
        # send_email commits its own EmailSendLog row; on an SES error it logs
        # "failed" and re-raises, so one bad address never blocks the rest.
        try:
            log = send_email(db, to=to, subject=subject, html=html, text=text, source=_SOURCE)
            if log.status in _HANDLED_STATUSES:
                delivered += 1
        except Exception:
            logger.exception("Survey digest: send to %s failed", to)
            db.rollback()

    if delivered == 0:
        logger.error("Survey digest: every send failed — releasing %d rows for the next run", len(rows))
        _release(db, rows)
        return 0

    logger.info("Survey digest sent — %d responses to %d/%d admins", len(rows), delivered, len(recipients))
    return delivered


def run_survey_digest(
    db: Session | None = None,
    resolve_emails: EmailResolver | None = None,
    now: datetime | None = None,
) -> int:
    """Scheduler entry point (and direct test invocation). Returns emails delivered.

    Tests pass their own session, resolver and clock. Only the scheduler path
    takes the leader lock — prod runs two uvicorn workers, each with its own
    scheduler, and both fire at 08:00.
    """
    resolve = resolve_emails or _supabase_email_resolver
    now = now or datetime.now(timezone.utc)
    if db is not None:
        return _send_digest(db, resolve, now)

    if not single_writer.is_leader(_LOCK):
        return 0
    session = get_session_factory()()
    try:
        return _send_digest(session, resolve, now)
    except Exception:
        logger.exception("Survey digest run failed")
        return 0
    finally:
        session.close()
