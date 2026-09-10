"""Turning a job row into what the monitoring screen shows.

Kept out of the router so the two decisions here can be read and tested on
their own: which of a job's quiet imperfections the list surfaces, and whether
its retry button can still do anything.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.video_pipeline import stage_progress
from src.video_pipeline.chapter_confidence import NO_MATCH
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.schemas import VideoJobDetail, VideoJobSummary
from src.video_pipeline.states import JobState


def chapters_of(job: WebinarVideoJob) -> list[dict]:
    """The chapter rows, minus anything that is not a mapping.

    ``chapters`` is JSONB written by the pipeline, so the screen treats its
    shape as unverified rather than trusting it and raising mid-render.
    """
    return [c for c in (job.chapters or []) if isinstance(c, dict)]


def to_summary(job: WebinarVideoJob) -> VideoJobSummary:
    webinar = job.webinar
    workshop = getattr(webinar, "workshop", None)
    chapters = chapters_of(job)
    return VideoJobSummary(
        id=job.id,
        webinar_id=job.webinar_id,
        webinar_name=getattr(webinar, "webinar_name", None),
        workshop_name=getattr(workshop, "name", None),
        zoom_webinar_id=getattr(webinar, "zoom_webinar_id", None),
        zoom_recording_uuid=job.zoom_recording_uuid,
        state=job.state,
        attempt=job.attempt,
        error=job.error,
        vimeo_video_id=job.vimeo_video_id,
        source_duration_seconds=job.source_duration_seconds,
        chapter_count=len(chapters),
        trim_fallback_used=job.trim_fallback_used,
        chapters_truncated=job.chapters_truncated,
        unconfirmed_chapters=sum(1 for c in chapters if c.get("confidence") == NO_MATCH),
        audit_only=job.audit_only,
        source_url=job.source_url,
        stage=stage_progress.current(job),
        captions_state=job.captions_state,
        captions_error=job.captions_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def archive_expired(job: WebinarVideoJob) -> bool:
    """Whether the Glacier copy of the original is already past its lifecycle.

    Decided here rather than in the browser: the answer depends on the clock,
    and a server-rendered page that disagrees with the same page a moment later
    in the client is both a hydration mismatch and a lie about a dead button.
    """
    expires_at = job.archive_expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def retry_status(job: WebinarVideoJob) -> tuple[bool, str | None]:
    """Whether retry can still work, and the reason it cannot.

    The archive is what makes retry possible for 90 days instead of Zoom's 7,
    so once its expiry has passed there is no source left to re-process and the
    only remaining fix is editing the video in Vimeo by hand.
    """
    if job.job_state is not JobState.FAILED:
        return False, f"Only failed jobs can be retried (job is '{job.state}')"
    if archive_expired(job):
        return False, "The archived source has expired — retry can no longer fetch it"
    return True, None


def to_detail(job: WebinarVideoJob) -> VideoJobDetail:
    retryable, blocked_reason = retry_status(job)
    return VideoJobDetail(
        **to_summary(job).model_dump(),
        ecs_task_arn=job.ecs_task_arn,
        trim_offset_seconds=job.trim_offset_seconds,
        vimeo_hash=job.vimeo_hash,
        chapters=job.chapters,
        failed_notified_at=job.failed_notified_at,
        archive_key=job.archive_key,
        archive_expires_at=job.archive_expires_at,
        archive_expired=archive_expired(job),
        frames_prefix=job.frames_prefix,
        retryable=retryable,
        retry_blocked_reason=blocked_reason,
        stage_events=stage_progress.events_of(job),
        stage_plan=stage_progress.expected_stages(job),
    )
