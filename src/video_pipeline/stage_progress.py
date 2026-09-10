"""The step-level timeline a job carries alongside its state.

``state`` says which half of the pipeline a job is in; it cannot say whether a
`processing` job has been stuck on a Zoom download for ten minutes or is
waiting on a Vimeo transcode. That difference only existed in CloudWatch, which
is not where anyone looks first. So each boundary records a `{stage, at}` event
on the row and the admin screen renders the list as a stepper.

Events mark a stage **starting**. That choice is what makes durations free: a
stage lasted until the next event, and the last event is what is happening now.
It also means an unfinished stage is visible as itself rather than as the
absence of the next one.

The ordered plan lives here rather than in the browser because which steps a
job will take is a property of the job — an audit run never deletes the Zoom
recording and never writes an embed code, so showing those as pending forever
would be wrong. Labels stay in the frontend; this module owns keys and order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.video_pipeline import thumbnail
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

QUEUED = "queued"
STARTING_TASK = "starting_task"
FETCHING_SOURCE = "fetching_source"
ARCHIVING_SOURCE = "archiving_source"
DETECTING_TRIM = "detecting_trim"
TRIMMING = "trimming"
SAMPLING_FRAMES = "sampling_frames"
UPLOADING_ARTIFACTS = "uploading_artifacts"
UPLOADING_TO_VIMEO = "uploading_to_vimeo"
SETTING_THUMBNAIL = "setting_thumbnail"
AWAITING_TRANSCODE = "awaiting_transcode"
DELETING_ZOOM_COPY = "deleting_zoom_copy"
LOADING_ARTIFACTS = "loading_artifacts"
SEGMENTING_TRANSCRIPT = "segmenting_transcript"
CLASSIFYING_FRAMES = "classifying_frames"
BUILDING_CHAPTERS = "building_chapters"
SETTING_CHAPTERS = "setting_chapters"
WRITING_EMBED_CODE = "writing_embed_code"
DONE = "done"

# Recorded when a run ends badly. Deliberately outside the plan: it is not a
# step anyone is waiting for, it is the end time of the step that broke.
FAILED = "failed"

# Every stage a job may pass through, in the order it would pass through them.
PLAN: tuple[str, ...] = (
    QUEUED,
    STARTING_TASK,
    FETCHING_SOURCE,
    ARCHIVING_SOURCE,
    DETECTING_TRIM,
    TRIMMING,
    SAMPLING_FRAMES,
    UPLOADING_ARTIFACTS,
    UPLOADING_TO_VIMEO,
    SETTING_THUMBNAIL,
    AWAITING_TRANSCODE,
    DELETING_ZOOM_COPY,
    LOADING_ARTIFACTS,
    SEGMENTING_TRANSCRIPT,
    CLASSIFYING_FRAMES,
    BUILDING_CHAPTERS,
    SETTING_CHAPTERS,
    WRITING_EMBED_CODE,
    DONE,
)

# A re-run after a partial failure walks stages a second time, and each pass is
# worth seeing. This only stops a pathological loop from growing the row without
# bound; oldest events are dropped first.
MAX_EVENTS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial() -> list[dict[str, str]]:
    """The timeline a brand-new — or freshly retried — job starts with."""
    return [{"stage": QUEUED, "at": _now()}]


def events_of(job: WebinarVideoJob) -> list[dict[str, Any]]:
    """The recorded events, minus anything that is not a usable event.

    ``stage_events`` is JSONB, so its shape is treated as unverified here for
    the same reason ``chapters`` is: a malformed row must not break the screen
    that exists to diagnose it.
    """
    raw = job.stage_events or []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("stage")]


def current(job: WebinarVideoJob) -> str | None:
    """The stage most recently started, or None for a job with no timeline."""
    events = events_of(job)
    return str(events[-1]["stage"]) if events else None


def expected_stages(job: WebinarVideoJob) -> list[str]:
    """The ordered stages *this* job will take.

    Three are dropped for the runs that never reach them. An audit run stops at
    Vimeo by design, a URL source has no Zoom recording to delete, and a
    workshop with no poster image has nothing to send to Vimeo — in each case
    the step is not skipped work, it is work that was never part of the job.
    """
    skipped: set[str] = set()
    if job.audit_only:
        skipped |= {DELETING_ZOOM_COPY, WRITING_EMBED_CODE}
    if job.source_url:
        skipped.add(DELETING_ZOOM_COPY)
    if not thumbnail.thumbnail_url(job):
        skipped.add(SETTING_THUMBNAIL)
    return [stage for stage in PLAN if stage not in skipped]


def record(db: Session, job: WebinarVideoJob, stage: str) -> None:
    """Stamp ``stage`` as started, and commit so a watcher sees it.

    Committing per stage is the point: the timeline exists to be read while the
    job is running, and a stage held in an uncommitted transaction is invisible
    to the streaming endpoint, which reads through its own session.

    Never raises. A timeline that cannot be written must not take down the run
    it was only describing — the pipeline's own logging is still there.
    """
    events = events_of(job)
    if events and events[-1].get("stage") == stage:
        return
    events.append({"stage": stage, "at": _now()})

    # Assigned as a new list rather than appended in place: SQLAlchemy compares
    # JSONB by identity, so a mutated list is not seen as dirty and never flushes.
    job.stage_events = events[-MAX_EVENTS:]
    try:
        db.commit()
    except Exception:
        logger.exception("Could not record stage %s for job %s", stage, job.id)
        db.rollback()
