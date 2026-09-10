"""Admin endpoints for the video pipeline — prefix /api/v1/admin/video-pipeline.

Monitoring, a retry button, and the lever that starts an audit run by hand.
All super_admin only (``AdminDep``).
Nothing here re-implements pipeline logic: retry goes through ``job_service`` so
the transition table stays the single authority on what a job may do, and the
detail view reports the chapters that were actually published rather than
rebuilding them, so what the screen shows is what Vimeo has.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.video_pipeline import frame_urls, job_service, job_views, manual_run, task_dispatch
from src.video_pipeline.manual_source import SourceError
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.schemas import (
    VideoJobDetail,
    VideoJobFrames,
    VideoJobList,
    VideoRunCreate,
    VideoRunStarted,
)
from src.video_pipeline.url_recording_fetch import UrlFetchError
from src.video_pipeline.states import JobState
from src.workshops.models import Webinar

router = APIRouter(prefix="/api/v1/admin/video-pipeline", tags=["video-pipeline-admin"])


def _load(db: Session, job_id: uuid.UUID) -> WebinarVideoJob:
    job = db.execute(
        select(WebinarVideoJob)
        .options(joinedload(WebinarVideoJob.webinar).joinedload(Webinar.workshop))
        .where(WebinarVideoJob.id == job_id)
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found")
    return job


@router.get("/jobs", response_model=VideoJobList)
def list_jobs(
    _admin: AdminDep,
    db: DbDep,
    state: str | None = Query(None, description="Filter by job state"),
    workshop_id: uuid.UUID | None = Query(None, description="Filter by workshop"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> VideoJobList:
    """Newest-first job list, optionally filtered to one state and workshop."""
    filters = []
    if state:
        try:
            filters.append(WebinarVideoJob.state == JobState(state).value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown state '{state}'",
            )
    if workshop_id:
        filters.append(
            WebinarVideoJob.webinar_id.in_(
                select(Webinar.id).where(Webinar.workshop_id == workshop_id)
            )
        )

    total = int(
        db.execute(
            select(func.count()).select_from(WebinarVideoJob).where(*filters)
        ).scalar_one()
    )
    jobs = (
        db.execute(
            select(WebinarVideoJob)
            .options(joinedload(WebinarVideoJob.webinar).joinedload(Webinar.workshop))
            .where(*filters)
            .order_by(WebinarVideoJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return VideoJobList(
        items=[job_views.to_summary(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=VideoJobDetail)
def get_job(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoJobDetail:
    """Full detail for one job — chapters, trim offset, task ARN, error."""
    return job_views.to_detail(_load(db, job_id))


@router.get("/jobs/{job_id}/frames", response_model=VideoJobFrames)
def get_job_frames(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoJobFrames:
    """Presigned URLs for the frames this job's chapters were built from.

    Short-lived and super_admin only: a frame is a still of a school's session,
    the same sensitivity as the recording it came from. An empty list is the
    normal answer for a job older than the 30-day frame lifecycle.
    """
    job = _load(db, job_id)
    frames = frame_urls.for_job(job)
    return VideoJobFrames(
        items=[frame.as_dict() for frame in frames],
        expires_in=frame_urls.EXPIRES_IN,
    )


@router.post("/runs", response_model=VideoRunStarted, status_code=status.HTTP_201_CREATED)
def create_run(payload: VideoRunCreate, _admin: AdminDep, db: DbDep) -> VideoRunStarted:
    """Start an audit run from a pasted Zoom reference or download URL.

    Audit-only by construction, not by a flag the caller passes: the run
    uploads to the Vimeo audit folder and stops. It writes no embed code and
    deletes no Zoom recording, so this endpoint cannot change what a school
    sees or destroy a source, however it is called.

    A recording that already has a job comes back 409 naming that job rather
    than being processed a second time — the same idempotency the webhook
    relies on, surfaced as an error the operator can act on.
    """
    if payload.webinar_id is not None and not db.get(Webinar, payload.webinar_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No webinar {payload.webinar_id}",
        )

    try:
        started = manual_run.start(db, source=payload.source, webinar_id=payload.webinar_id)
    except manual_run.RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (SourceError, UrlFetchError, manual_run.RunError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    db.refresh(started.job)
    return VideoRunStarted(job=job_views.to_detail(started.job), dispatched=started.dispatched)


@router.post("/jobs/{job_id}/retry", response_model=VideoJobDetail)
def retry_job(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoJobDetail:
    """Re-arm a failed job and dispatch it if there is capacity.

    Only `failed` jobs are retryable. An active job is already someone's
    responsibility, and re-dispatching one would put two tasks on the same
    recording; a published one has nothing to redo. A job whose archived source
    has expired is refused for a different reason: there is nothing left to
    process, so re-arming it would only produce a second identical failure.
    """
    job = _load(db, job_id)
    retryable, blocked_reason = job_views.retry_status(job)
    if not retryable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blocked_reason)

    job_service.retry(db, job)
    # Over the cap, dispatch declines and the sweeper picks the job up later.
    task_dispatch.dispatch(db, job)
    db.refresh(job)
    return job_views.to_detail(job)
