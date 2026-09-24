"""Trailer reels of a job: ask for one, list them, upload a finished one to Vimeo.

The render itself runs in the ECS reel task (``reel_task``); this module owns
the rows and the rules around them. A job renders one reel at a time, so an
admin who asks twice does not pay Bedrock, Transcribe and a Fargate task twice.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import settings
from src.integrations import vimeo, vimeo_upload
from src.storage.s3_client import s3_client
from src.video_pipeline import frame_urls, reel_sources, task_dispatch
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import (
    ACTIVE_STATES,
    FAILED,
    ORIENTATIONS,
    READY,
    WebinarVideoReel,
)
from src.video_pipeline.reel_schemas import VideoReel
from src.video_pipeline.video_title import MAX_TITLE

logger = logging.getLogger(__name__)

PREVIEW_EXPIRES_IN = 3600
# A task that has not moved a reel on in this long is gone: the slowest stage,
# rendering a minute of 1080p, takes a few minutes.
STALE_AFTER = timedelta(minutes=60)
ALREADY_ACTIVE = "A reel of this recording is already being made."
STALE_ERROR = "The reel task stopped reporting progress — it was likely interrupted."


class ReelConflict(Exception):
    """The request cannot be honoured in the reel's or job's current state."""


class ReelUploadError(Exception):
    """Vimeo or S3 refused the upload."""


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _expire_stale(db: Session, reels: list[WebinarVideoReel]) -> None:
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    stale = [r for r in reels if r.state in ACTIVE_STATES and _aware(r.updated_at) < cutoff]
    for reel in stale:
        reel.state, reel.error = FAILED, STALE_ERROR
    if stale:
        db.commit()


def list_reels(db: Session, job: WebinarVideoJob) -> list[WebinarVideoReel]:
    """The job's reels, newest first, with abandoned renders marked failed."""
    reels = list(db.scalars(
        select(WebinarVideoReel)
        .where(WebinarVideoReel.job_id == job.id)
        .order_by(WebinarVideoReel.created_at.desc())
    ))
    _expire_stale(db, reels)
    return reels


def create_reel(db: Session, job: WebinarVideoJob, orientation: str,
                prompt: str | None) -> WebinarVideoReel:
    """Insert a reel and launch its task. Raises ReelConflict when not possible."""
    if orientation not in ORIENTATIONS:
        raise ValueError(f"Unknown orientation {orientation!r}")
    reason = reel_sources.blocked_reason(job)
    if reason:
        raise ReelConflict(reason)
    if any(r.state in ACTIVE_STATES for r in list_reels(db, job)):
        raise ReelConflict(ALREADY_ACTIVE)

    reel = WebinarVideoReel(job_id=job.id, orientation=orientation,
                            prompt=(prompt or "").strip() or None)
    db.add(reel)
    try:
        db.commit()
    except IntegrityError as exc:
        # Another request inserted this job's active reel since the check above.
        db.rollback()
        raise ReelConflict(ALREADY_ACTIVE) from exc
    task_dispatch.dispatch_reel(db, reel)
    db.refresh(reel)
    return reel


def reel_title(job: WebinarVideoJob, orientation: str) -> str:
    """The reel's Vimeo name, within Vimeo's cap: the webinar name gives way, the suffix stays."""
    webinar = (job.webinar.webinar_name if job.webinar else None) or "Webinar"
    suffix = f" — trailer ({orientation})"
    return f"{webinar[:MAX_TITLE - len(suffix)].rstrip()}{suffix}"


def upload_to_vimeo(db: Session, reel: WebinarVideoReel, job: WebinarVideoJob) -> WebinarVideoReel:
    """Upload a ready reel as an embed-only Vimeo video. Synchronous: ~40 MB."""
    if reel.state != READY or not reel.s3_key:
        raise ReelConflict("Only a finished reel can be uploaded.")
    if reel.vimeo_video_id:
        raise ReelConflict("This reel is already on Vimeo.")

    name = reel_title(job, reel.orientation)
    with tempfile.TemporaryDirectory(prefix="video-reel-upload-") as tmp:
        path = Path(tmp) / "reel.mp4"
        try:
            s3_client().download_file(settings.s3_bucket_name, reel.s3_key, str(path))
            created = vimeo_upload.create_video(
                path, name, description=reel.hook_title or None,
                folder_uri=vimeo_upload.reel_folder_uri() or None)
        except vimeo.VimeoError as exc:
            raise ReelUploadError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Reel %s upload failed", reel.id)
            raise ReelUploadError(f"Could not upload the reel — {exc}") from exc

    reel.vimeo_video_id, reel.vimeo_hash = created["video_id"], created.get("hash") or None
    db.commit()
    logger.info("Reel %s uploaded to Vimeo — video=%s", reel.id, reel.vimeo_video_id)
    return reel


def to_view(reel: WebinarVideoReel) -> VideoReel:
    preview = frame_urls.object_url(reel.s3_key, PREVIEW_EXPIRES_IN) \
        if reel.state == READY and reel.s3_key else None
    expires_in = frame_urls.url_lifetime(PREVIEW_EXPIRES_IN) if preview else None
    vimeo_url = None
    if reel.vimeo_video_id:
        suffix = f"/{reel.vimeo_hash}" if reel.vimeo_hash else ""
        vimeo_url = f"https://vimeo.com/{reel.vimeo_video_id}{suffix}"
    return VideoReel(
        id=reel.id, job_id=reel.job_id, orientation=reel.orientation, prompt=reel.prompt,
        state=reel.state, stage=reel.stage, hook_title=reel.hook_title,
        duration_seconds=float(reel.duration_seconds) if reel.duration_seconds is not None
        else None,
        preview_url=preview, preview_expires_in=expires_in,
        vimeo_video_id=reel.vimeo_video_id, vimeo_url=vimeo_url, error=reel.error,
        created_at=reel.created_at, updated_at=reel.updated_at,
    )
