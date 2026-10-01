"""Admin endpoints for trailer reels — prefix /api/v1/admin/video-pipeline.

Ask for a ~60 second reel of a published job, review and refine its clips as a
draft, send it to render, preview it from S3 and upload it to Vimeo. All
super_admin only (``AdminDep``); the rules live in ``reel_draft`` and
``reel_service``. Model turns on a draft run after the response
(``reel_draft_task``), so the screen polls the list while one is `drafting`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.video_pipeline import reel_draft, reel_draft_task, reel_service, reel_sources
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import ACTIVE_STATES, DRAFTING, WebinarVideoReel
from src.video_pipeline.reel_schemas import (
    VideoReel,
    VideoReelCreate,
    VideoReelDraftEdit,
    VideoReelList,
    VideoReelMessageCreate,
    VideoReelRender,
)

router = APIRouter(prefix="/api/v1/admin/video-pipeline", tags=["video-pipeline-admin"])


def _load_job(db: Session, job_id: uuid.UUID) -> WebinarVideoJob:
    job = db.execute(
        select(WebinarVideoJob)
        .options(joinedload(WebinarVideoJob.webinar))
        .where(WebinarVideoJob.id == job_id)
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found")
    return job


def _load_reel(db: Session, reel_id: uuid.UUID) -> WebinarVideoReel:
    reel = db.get(WebinarVideoReel, reel_id)
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    return reel


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/jobs/{job_id}/reels", response_model=VideoReelList)
def list_reels(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoReelList:
    """The job's reels, newest first, and why a new one cannot be made (if so)."""
    job = _load_job(db, job_id)
    reels = reel_service.list_reels(db, job)
    # The screen polls while a reel renders or drafts; the reason cannot change
    # meanwhile and costs S3 (and maybe Zoom) calls, so it is only worked out when idle.
    busy = any(r.state in ACTIVE_STATES or r.state == DRAFTING for r in reels)
    return VideoReelList(
        items=[reel_service.to_view(r) for r in reels],
        blocked_reason=None if busy else reel_sources.blocked_reason(job),
    )


@router.post("/jobs/{job_id}/reels", response_model=VideoReel,
             status_code=status.HTTP_201_CREATED)
def create_reel(job_id: uuid.UUID, body: VideoReelCreate, background: BackgroundTasks,
                _admin: AdminDep, db: DbDep) -> VideoReel:
    """Start a draft; its first cut is made after the response. 409 when blocked."""
    job = _load_job(db, job_id)
    try:
        reel = reel_draft.create_draft(db, job, body.prompt)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    background.add_task(reel_draft_task.run_turn, reel.id)
    return reel_service.to_view(reel)


@router.post("/reels/{reel_id}/messages", response_model=VideoReel)
def send_message(reel_id: uuid.UUID, body: VideoReelMessageCreate,
                 background: BackgroundTasks, _admin: AdminDep, db: DbDep) -> VideoReel:
    """Ask for a change to a draft; revised after the response. 409 busy/not a draft."""
    reel = _load_reel(db, reel_id)
    try:
        reel = reel_draft.add_message(db, reel, body.text)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    background.add_task(reel_draft_task.run_turn, reel.id)
    return reel_service.to_view(reel)


@router.patch("/reels/{reel_id}/draft", response_model=VideoReel)
def edit_draft(reel_id: uuid.UUID, body: VideoReelDraftEdit, _admin: AdminDep,
               db: DbDep) -> VideoReel:
    """Retitle, reorder or remove clips by hand — no model call."""
    reel = _load_reel(db, reel_id)
    try:
        reel = reel_draft.edit_draft(db, reel, hook_title=body.hook_title,
                                     segment_order=body.segment_order)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from exc
    return reel_service.to_view(reel)


@router.post("/reels/{reel_id}/render", response_model=VideoReel)
def render_reel(reel_id: uuid.UUID, body: VideoReelRender, _admin: AdminDep,
                db: DbDep) -> VideoReel:
    """Render the draft's clips. 409 when blocked, busy, or another reel renders."""
    reel = _load_reel(db, reel_id)
    job = _load_job(db, reel.job_id)
    try:
        reel = reel_draft.start_render(db, reel, job, body.orientation)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    return reel_service.to_view(reel)


@router.delete("/reels/{reel_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_reel(reel_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> Response:
    """Delete a draft. 409 once it has been sent to render."""
    reel = _load_reel(db, reel_id)
    try:
        reel_draft.discard(db, reel)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reels/{reel_id}/vimeo", response_model=VideoReel)
def upload_reel(reel_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoReel:
    """Upload a finished reel to Vimeo (embed-only). 409 not ready/already; 502 Vimeo."""
    reel = _load_reel(db, reel_id)
    job = _load_job(db, reel.job_id)
    try:
        reel = reel_service.upload_to_vimeo(db, reel, job)
    except reel_service.ReelConflict as exc:
        raise _conflict(exc) from exc
    except reel_service.ReelUploadError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return reel_service.to_view(reel)
