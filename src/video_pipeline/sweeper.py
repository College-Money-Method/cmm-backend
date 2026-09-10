"""Periodic pass that keeps the pipeline moving without a webhook to prompt it.

Four jobs, in this order:

1. **Free capacity.** A task that dies without advancing state — an OOM kill, a
   spot reclaim, a crash before the first checkpoint — leaves its job in
   `processing` forever. The concurrency cap counts `processing`, so three of
   those deadlock the whole pipeline. Anything older than the stuck threshold is
   failed, which both alerts ops and returns the slot.
2. **Drain the backlog.** Jobs that arrived while the cap was full are still
   `pending` with nobody about to retry them. Dispatch as many as now fit.
3. **Publish what is waiting.** The ECS task ends at `chaptering`; nothing else
   picks those jobs up, so this is the only thing that finishes them.
4. **Caption what is published.** Vimeo writes the English transcript minutes
   after the transcode and announces it to nobody, so the only way to find it is
   to keep looking.

Order matters: freeing slots before dispatching means one pass can recover from
a full deadlock instead of two. Publishing and captioning come last because they
are the slow steps — minutes of model and Vimeo calls each — and the first two
must not wait behind them. Captioning comes after publishing because a replay
being live matters more than its Spanish subtitles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.config import settings
from src.db.base import get_session_factory
from src.video_pipeline import caption_task, job_service, publish_service, task_dispatch
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState

logger = logging.getLogger(__name__)

# A 90-minute recording transcodes and uploads well inside this. Generous on
# purpose: failing a job that is merely slow costs a re-download of the source.
_STUCK_AFTER_MINUTES = 90

# How many jobs to chapter in one sweep. Each costs a vision call per sampled
# frame, so a whole evening's backlog in one pass would hold the scheduler slot
# for the better part of an hour; the next sweep picks up the remainder.
_PUBLISH_BATCH = 3

# How many published replays to caption in one sweep. One: a caption run is a
# Bedrock call per chunk across three languages, and unlike publishing there is
# nothing waiting on it — the next sweep is five minutes away.
_CAPTION_BATCH = 1


def fail_stuck_jobs(db, stuck_after_minutes: int = _STUCK_AFTER_MINUTES) -> int:
    """Fail `processing` jobs that have not moved in ``stuck_after_minutes``."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stuck_after_minutes)
    stuck = list(
        db.execute(
            select(WebinarVideoJob).where(
                WebinarVideoJob.state == JobState.PROCESSING.value,
                WebinarVideoJob.updated_at < cutoff,
            )
        ).scalars()
    )

    for job in stuck:
        logger.error(
            "Video job stuck in processing — failing to free capacity job=%s task=%s",
            job.id,
            job.ecs_task_arn,
        )
        job_service.fail(
            db,
            job,
            f"Task made no progress for over {stuck_after_minutes} minutes; "
            "presumed dead and failed by the sweeper.",
        )
    return len(stuck)


def dispatch_pending_jobs(db) -> int:
    """Launch as many `pending` jobs as the concurrency cap currently allows."""
    capacity = settings.video_pipeline_max_concurrent - job_service.count_in_flight(db)
    if capacity <= 0:
        return 0

    dispatched = 0
    for job in job_service.list_pending(db, limit=capacity):
        if task_dispatch.dispatch(db, job):
            dispatched += 1
    return dispatched


def publish_chaptering_jobs(db) -> int:
    """Chapter and publish the jobs the ECS task left in `chaptering`.

    One job's failure fails only that job: the rest of the batch still runs, and
    the failure is recorded where an admin can see and retry it. Re-running a
    job that partly published is safe — the frames are still in S3 and the
    chapter list is replaced wholesale — so nothing here tries to resume.
    """
    published = 0
    for job in job_service.list_chaptering(db, limit=_PUBLISH_BATCH):
        try:
            publish_service.publish(db, job)
            published += 1
        except Exception as exc:
            logger.exception("Chaptering failed — job=%s error=%s", job.id, exc)
            db.rollback()
            job_service.fail(db, job, f"Chaptering failed: {exc}")
    return published


def caption_published_jobs(db) -> int:
    """Translate the captions of replays whose English track Vimeo has written.

    Returns the number of jobs that finished captioning in this sweep — a job
    that looked and found no transcript yet is not counted, because nothing
    happened to it beyond another check.
    """
    captioned = 0
    for job in caption_task.due(db, limit=_CAPTION_BATCH):
        try:
            if caption_task.run(db, job) == caption_task.COMPLETED:
                captioned += 1
        except Exception as exc:  # noqa: BLE001 — a published replay stays published
            logger.exception("Captioning failed — job=%s error=%s", job.id, exc)
            db.rollback()
    return captioned


def run_video_pipeline_sweep() -> None:
    """Scheduler entry point. Never raises — nothing upstream would catch it."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        failed = fail_stuck_jobs(db)
        dispatched = dispatch_pending_jobs(db)
        published = publish_chaptering_jobs(db)
        captioned = caption_published_jobs(db)
        if failed or dispatched or published or captioned:
            logger.info(
                "Video pipeline sweep — stuck_failed=%d dispatched=%d published=%d "
                "captioned=%d",
                failed,
                dispatched,
                published,
                captioned,
            )
    except Exception as exc:
        logger.exception("Video pipeline sweep failed — error=%s", exc)
    finally:
        db.close()
