"""Admin endpoints for trailer reels — prefix /api/v1/admin/video-pipeline.

Ask for a ~60 second reel of a published job, watch it render, preview it from
S3 and upload it to Vimeo. All super_admin only (``AdminDep``); the rules live
in ``reel_service``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.video_pipeline import reel_service, reel_sources
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import ACTIVE_STATES, WebinarVideoReel
from src.video_pipeline.reel_schemas import VideoReel, VideoReelCreate, VideoReelList

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


@router.get("/jobs/{job_id}/reels", response_model=VideoReelList)
def list_reels(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoReelList:
    """The job's reels, newest first, and why a new one cannot be made (if so)."""
    job = _load_job(db, job_id)
    reels = reel_service.list_reels(db, job)
    # The screen polls while a reel renders; the reason cannot change meanwhile
    # and costs S3 (and maybe Zoom) calls, so it is only worked out when idle.
    rendering = any(r.state in ACTIVE_STATES for r in reels)
    return VideoReelList(
        items=[reel_service.to_view(r) for r in reels],
        blocked_reason=None if rendering else reel_sources.blocked_reason(job),
    )


@router.post("/jobs/{job_id}/reels", response_model=VideoReel,
             status_code=status.HTTP_201_CREATED)
def create_reel(job_id: uuid.UUID, body: VideoReelCreate, _admin: AdminDep,
                db: DbDep) -> VideoReel:
    """Start a reel. 409 when the job cannot have one or already has one rendering."""
    job = _load_job(db, job_id)
    try:
        reel = reel_service.create_reel(db, job, body.orientation, body.prompt)
    except reel_service.ReelConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return reel_service.to_view(reel)


@router.post("/reels/{reel_id}/vimeo", response_model=VideoReel)
def upload_reel(reel_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> VideoReel:
    """Upload a finished reel to Vimeo (embed-only). 409 not ready/already; 502 Vimeo."""
    reel = db.get(WebinarVideoReel, reel_id)
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    job = _load_job(db, reel.job_id)
    try:
        reel = reel_service.upload_to_vimeo(db, reel, job)
    except reel_service.ReelConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except reel_service.ReelUploadError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return reel_service.to_view(reel)
