"""Trailer reels of a job: list them, upload a finished one to Vimeo.

A reel is made as a draft and sent to render from ``reel_draft``; the render
itself runs in the ECS reel task (``reel_task``). This module owns listing,
the view and the Vimeo upload. A job renders one reel at a time, so an admin
who asks twice does not pay Transcribe and a Fargate task twice.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.integrations import vimeo, vimeo_upload
from src.storage.s3_client import s3_client
from src.video_pipeline import frame_urls
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import (
    ACTIVE_STATES,
    DRAFT,
    DRAFTING,
    FAILED,
    READY,
    WebinarVideoReel,
)
from src.video_pipeline.reel_schemas import ReelSelection, VideoReel
from src.video_pipeline.trailer_select import Selection, rule_problems
from src.video_pipeline.video_title import MAX_TITLE

logger = logging.getLogger(__name__)

PREVIEW_EXPIRES_IN = 3600
# A task that has not moved a reel on in this long is gone: the slowest stage,
# rendering a minute of 1080p, takes a few minutes.
STALE_AFTER = timedelta(minutes=60)
# A model turn is one Sonnet call and a retry, a minute or two at most; one
# this old died with the API process that ran it (a deploy, a restart).
DRAFTING_STALE_AFTER = timedelta(minutes=10)
ALREADY_ACTIVE = "A reel of this recording is already being made."
STALE_ERROR = "The reel task stopped reporting progress — it was likely interrupted."
DRAFTING_STALE_ERROR = "That change stopped responding before it finished — send it again."


class ReelConflict(Exception):
    """The request cannot be honoured in the reel's or job's current state."""


class ReelUploadError(Exception):
    """Vimeo or S3 refused the upload."""


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _expire_stale(db: Session, reels: list[WebinarVideoReel]) -> None:
    now = datetime.now(timezone.utc)
    stale = [r for r in reels if r.state in ACTIVE_STATES
             and _aware(r.updated_at) < now - STALE_AFTER]
    for reel in stale:
        reel.state, reel.error = FAILED, STALE_ERROR
    if stale:
        db.commit()
    # A lost model turn leaves the draft as it was before the request: the
    # admin keeps the reel and the conversation, and can ask again. Conditional
    # on the row being unchanged since it was read, so a turn that finishes at
    # the same moment keeps its result instead of being overwritten.
    lost = [r for r in reels if r.state == DRAFTING
            and _aware(r.updated_at) < now - DRAFTING_STALE_AFTER]
    for reel in lost:
        seen = reel.updated_at
        db.refresh(reel, with_for_update=True)
        if reel.state == DRAFTING and reel.updated_at == seen:
            reel.state, reel.error = DRAFT, DRAFTING_STALE_ERROR
    if lost:
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


def _selection_view(stored: dict | None) -> tuple[ReelSelection | None, list[str]]:
    if not stored:
        return None, []
    selection = Selection.from_dict(stored)
    view = ReelSelection(
        hook_title=selection.hook_title,
        total_seconds=round(selection.total_seconds, 2),
        segments=[{**segment.__dict__, "duration": round(segment.duration, 2)}
                  for segment in selection.segments],
    )
    return view, rule_problems(selection)


def to_view(reel: WebinarVideoReel) -> VideoReel:
    preview = frame_urls.object_url(reel.s3_key, PREVIEW_EXPIRES_IN) \
        if reel.state == READY and reel.s3_key else None
    expires_in = frame_urls.url_lifetime(PREVIEW_EXPIRES_IN) if preview else None
    vimeo_url = None
    if reel.vimeo_video_id:
        suffix = f"/{reel.vimeo_hash}" if reel.vimeo_hash else ""
        vimeo_url = f"https://vimeo.com/{reel.vimeo_video_id}{suffix}"
    selection, problems = _selection_view(reel.selection)
    return VideoReel(
        id=reel.id, job_id=reel.job_id, orientation=reel.orientation, prompt=reel.prompt,
        state=reel.state, stage=reel.stage, hook_title=reel.hook_title,
        selection=selection, messages=reel.messages or [], problems=problems,
        duration_seconds=float(reel.duration_seconds) if reel.duration_seconds is not None
        else None,
        preview_url=preview, preview_expires_in=expires_in,
        vimeo_video_id=reel.vimeo_video_id, vimeo_url=vimeo_url, error=reel.error,
        created_at=reel.created_at, updated_at=reel.updated_at,
    )
