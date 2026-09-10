"""Create, advance, fail and retry ``WebinarVideoJob`` rows.

Every write to a job's ``state`` goes through here so the transition table in
``states.py`` is enforced in one place. Callers pass the fields they learned
alongside the transition (trim offset, Vimeo id, chapters) rather than mutating
the row and committing separately, which keeps "what happened" and "where the
job now is" in a single transaction.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.video_pipeline import stage_progress
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState, assert_legal

logger = logging.getLogger(__name__)


def get_by_recording_uuid(db: Session, zoom_recording_uuid: str) -> WebinarVideoJob | None:
    return db.execute(
        select(WebinarVideoJob).where(WebinarVideoJob.zoom_recording_uuid == zoom_recording_uuid)
    ).scalar_one_or_none()


def create_from_recording(
    db: Session,
    *,
    zoom_recording_uuid: str,
    webinar_id: uuid.UUID | None = None,
    source_url: str | None = None,
    audit_only: bool = False,
) -> tuple[WebinarVideoJob, bool]:
    """Insert a `pending` job for a recording, or return the existing one.

    Returns ``(job, created)``. Idempotent on ``zoom_recording_uuid``: Zoom
    retries webhooks and the reconcile sweep deliberately re-offers recordings
    it has already seen, so both paths land here and exactly one row survives.

    The guarantee comes from the UNIQUE constraint, not from the pre-check — a
    check-then-insert would still race two concurrent webhook deliveries. The
    insert runs inside a SAVEPOINT so a losing race rolls back only the failed
    insert, leaving the caller's transaction usable.

    ``webinar_id`` is optional because an audit run has nothing to publish to,
    and ``source_url`` carries a download URL for a source that is not a Zoom
    recording. Such a source has no identity of its own, so its caller supplies
    a synthetic ``zoom_recording_uuid`` rather than the constraint being
    relaxed to let it through.
    """
    existing = get_by_recording_uuid(db, zoom_recording_uuid)
    if existing is not None:
        return existing, False

    job = WebinarVideoJob(
        webinar_id=webinar_id,
        zoom_recording_uuid=zoom_recording_uuid,
        source_url=source_url,
        audit_only=audit_only,
        state=JobState.PENDING.value,
        stage_events=stage_progress.initial(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        # Lost the race: another delivery inserted the same recording first.
        # The SAVEPOINT rollback already detaches the pending instance, so
        # expunge is only for the case where it did not — calling it on an
        # already-detached instance raises and would mask the recovery.
        if job in db:
            db.expunge(job)
        winner = get_by_recording_uuid(db, zoom_recording_uuid)
        if winner is None:  # pragma: no cover - only if the constraint is gone
            raise
        return winner, False

    db.commit()
    logger.info(
        "Video job created — job=%s webinar=%s recording=%s",
        job.id,
        webinar_id,
        zoom_recording_uuid,
    )
    return job, True


def advance(
    db: Session,
    job: WebinarVideoJob,
    target: JobState,
    **fields: Any,
) -> WebinarVideoJob:
    """Move ``job`` to ``target``, writing ``fields`` in the same transaction.

    Raises :class:`~src.video_pipeline.states.IllegalTransition` if the move is
    not one the pipeline defines.
    """
    assert_legal(job.job_state, target)
    for name, value in fields.items():
        setattr(job, name, value)
    job.state = target.value
    db.commit()
    logger.info("Video job advanced — job=%s state=%s", job.id, target.value)
    return job


def fail(db: Session, job: WebinarVideoJob, error: str) -> WebinarVideoJob:
    """Move ``job`` to `failed`, record ``error``, and alert ops exactly once.

    The webinar is left untouched — a failed job means "no replay yet", never a
    broken one. Notification is best-effort and cannot raise, so a broken alert
    path never blocks the state transition that records the failure.
    """
    advance(db, job, JobState.FAILED, error=error[:4000])
    # Closes the timeline: the stage before this one is where the run broke, and
    # without an end time it would look like it is still running.
    stage_progress.record(db, job, stage_progress.FAILED)

    from src.video_pipeline.notify import notify_failure  # local: avoids an import cycle

    notify_failure(db, job)
    return job


def retry(db: Session, job: WebinarVideoJob) -> WebinarVideoJob:
    """Re-arm a failed job: back to `pending`, attempt incremented, slate cleared.

    ``failed_notified_at`` is cleared deliberately — if this attempt fails too,
    that is a new failure and ops should hear about it again. The stage timeline
    restarts for the same reason: interleaving a second run's steps with the
    failed run's would describe a sequence that never happened.
    """
    return advance(
        db,
        job,
        JobState.PENDING,
        attempt=job.attempt + 1,
        error=None,
        failed_notified_at=None,
        ecs_task_arn=None,
        stage_events=stage_progress.initial(),
    )


def list_chaptering(db: Session, limit: int = 5) -> list[WebinarVideoJob]:
    """Oldest-first `chaptering` jobs, for the sweeper to publish.

    Small limit by default: chaptering is minutes of Bedrock and Vimeo calls per
    job, and it runs inside the sweep, so a large batch would hold the scheduler
    slot long past the sweep interval.
    """
    return list(
        db.execute(
            select(WebinarVideoJob)
            .where(WebinarVideoJob.state == JobState.CHAPTERING.value)
            .order_by(WebinarVideoJob.created_at)
            .limit(limit)
        ).scalars()
    )


def count_in_flight(db: Session) -> int:
    """Number of jobs currently occupying an ECS task slot.

    Only `processing` counts. `chaptering` runs inside the API process and
    consumes no task capacity, so including it would idle the cluster.
    """
    return int(
        db.execute(
            select(func.count())
            .select_from(WebinarVideoJob)
            .where(WebinarVideoJob.state == JobState.PROCESSING.value)
        ).scalar_one()
    )


def list_pending(db: Session, limit: int = 20) -> list[WebinarVideoJob]:
    """Oldest-first `pending` jobs, for the sweeper to dispatch as capacity frees."""
    return list(
        db.execute(
            select(WebinarVideoJob)
            .where(WebinarVideoJob.state == JobState.PENDING.value)
            .order_by(WebinarVideoJob.created_at)
            .limit(limit)
        ).scalars()
    )
