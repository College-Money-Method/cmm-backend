"""Vimeo's English transcript, for a recording Zoom delivered without one.

The topic pass is what finds a webinar's sections when the deck does not title
them, and it reads the transcript. Zoom's transcript is optional in practice: it
is written after the recording, so a fetch triggered by `recording.completed`
can land before it exists, and the Zoom copy is deleted once the replay is
live — so the late `recording.transcript_completed` never gets a chance. A
presenter who never shows a title card then gets a single chapter.

Vimeo transcribes every upload on its own, and it transcribes the file we sent
it: the trimmed video. Its cues are on the same clock as the frames, so they
drop in where Zoom's re-based cues would have gone. The track arrives a few
minutes after the transcode, with no event to announce it, so chaptering waits
a bounded time for it before settling for frames alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.integrations import vimeo
from src.integrations.vimeo import VimeoError
from src.video_pipeline import artifact_store
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.transcript import Cue, VttError, parse_cues

logger = logging.getLogger(__name__)

SOURCE_LANGUAGE = "en"


class TranscriptPending(RuntimeError):
    """Vimeo has not written the transcript yet; try this job again next sweep."""


def _borrow(video_ref: str) -> list[Cue]:
    """Vimeo's English track as cues, or ``[]`` when there is none to read."""
    try:
        content, _name = vimeo.download_source_track(video_ref, SOURCE_LANGUAGE)
    except VimeoError as exc:
        # A missing track is the expected "not yet". Anything else — auth, rate
        # limit, an outage — waits the same way, but loudly, so a broken
        # integration is not mistaken for Vimeo taking its time.
        level = logging.INFO if exc.status in (None, 404) else logging.WARNING
        logger.log(level, "No %s transcript on Vimeo for %s yet — %s", SOURCE_LANGUAGE, video_ref, exc)
        return []
    try:
        return parse_cues(content)
    except VttError as exc:
        logger.warning("Vimeo's transcript for %s could not be parsed — %s", video_ref, exc)
        return []


def _still_worth_waiting(job: WebinarVideoJob, now: datetime | None = None) -> bool:
    """Whether the job has waited less than the configured budget.

    Measured from ``updated_at``: nothing writes the row while it waits, so that
    is when chaptering first picked it up.
    """
    now = now or datetime.now(timezone.utc)
    started = job.updated_at
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return now - started < timedelta(minutes=settings.video_transcript_wait_minutes)


def cues_for(job: WebinarVideoJob, video_ref: str) -> list[Cue]:
    """Cues from Vimeo for a job whose own transcript is empty.

    Written back to ``transcript.json`` when found, so everything that reads the
    job's transcript after chaptering — the Q&A answer extraction — reads these
    too. A failed write is logged, not raised: the chapters do not depend on it.

    Raises:
        TranscriptPending: Vimeo has no track yet and the wait is not over.
    """
    cues = _borrow(video_ref)
    if cues:
        logger.info(
            "Zoom supplied no transcript for job %s — chaptering from Vimeo's (%d cues)",
            job.id,
            len(cues),
        )
        if job.frames_prefix:
            try:
                artifact_store.save_transcript(job.frames_prefix, cues)
            except artifact_store.ArtifactError as exc:
                logger.warning("Could not keep Vimeo's transcript for job %s — %s", job.id, exc)
        return cues

    if _still_worth_waiting(job):
        raise TranscriptPending(f"Waiting for Vimeo's {SOURCE_LANGUAGE} transcript of {video_ref}")

    logger.warning(
        "No transcript from Zoom or Vimeo for job %s after %d minutes — chaptering from frames alone",
        job.id,
        settings.video_transcript_wait_minutes,
    )
    return []


__all__ = ["TranscriptPending", "cues_for"]
