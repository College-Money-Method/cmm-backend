"""Translated captions for a replay that is already live.

Vimeo writes the English transcript itself, some minutes after the transcode
finishes, and offers no webhook to say when — the main API has no event for it,
and polling is what its own documentation tells you to do. Holding a finished
video back while waiting would delay the thing schools are actually waiting for,
so the pipeline publishes first and comes back for the captions afterwards.

That is why this is not a pipeline state. ``published`` is terminal and stays
terminal: it is the one word that means a school's page has a working player on
it, and a caption failure must not be able to take that back. Caption progress
lives in its own columns beside it.

The work itself is the admin Video CC feature, unchanged — same translation,
same cost ledger, same replace-the-track behaviour. This module only decides
*when* to run it and records how it went.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.config import SUPPORTED_LOCALES, settings
from src.content import video_cc_service
from src.content.video_cc_jobs import VideoCcJob
from src.integrations import vimeo
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
# Vimeo never produced an English transcript to translate. An outcome, not a
# fault — nothing here can make Vimeo transcribe a recording it did not.
SKIPPED = "skipped"

# A `running` row older than this was left behind by a process that died
# mid-translation. Reclaiming it is safe: a caption run replaces the track for
# each language wholesale, so a partial run repeated finishes the job.
_STALE_RUNNING_MINUTES = 60

SOURCE_LANGUAGE = "en"


def locales() -> list[str]:
    """The configured target languages, minus anything unsupported.

    A typo in the environment drops one language with a warning rather than
    failing the run: three languages minus one is still worth publishing.
    """
    wanted = [part.strip() for part in (settings.video_caption_locales or "").split(",")]
    keep: list[str] = []
    for locale in wanted:
        if not locale:
            continue
        if locale not in SUPPORTED_LOCALES:
            logger.warning("Ignoring unsupported caption locale %r", locale)
            continue
        keep.append(locale)
    return keep


def video_ref(job: WebinarVideoJob) -> str | None:
    """The Vimeo reference the caption run works on, private hash included."""
    if not job.vimeo_video_id:
        return None
    return f"{job.vimeo_video_id}:{job.vimeo_hash}" if job.vimeo_hash else job.vimeo_video_id


def due(db: Session, limit: int) -> list[WebinarVideoJob]:
    """Published jobs whose captions still need attention, oldest first."""
    stale = datetime.now(timezone.utc) - timedelta(minutes=_STALE_RUNNING_MINUTES)
    return list(
        db.execute(
            select(WebinarVideoJob)
            .where(
                WebinarVideoJob.state == JobState.PUBLISHED.value,
                WebinarVideoJob.vimeo_video_id.is_not(None),
                or_(
                    WebinarVideoJob.captions_state == PENDING,
                    (WebinarVideoJob.captions_state == RUNNING)
                    & (WebinarVideoJob.updated_at < stale),
                ),
            )
            .order_by(WebinarVideoJob.updated_at)
            .limit(limit)
        ).scalars()
    )


def has_source_track(ref: str) -> bool:
    """Whether Vimeo has an English track on the video yet."""
    tracks = vimeo.list_text_tracks(ref)
    return any(
        (t.get("language") or "").lower().startswith(SOURCE_LANGUAGE) for t in tracks
    )


def _settle(db: Session, job: WebinarVideoJob, state: str, error: str | None = None) -> str:
    job.captions_state = state
    job.captions_error = error
    if state in (COMPLETED, FAILED, SKIPPED):
        job.captions_completed_at = datetime.now(timezone.utc)
    db.commit()
    return state


def run(db: Session, job: WebinarVideoJob) -> str:
    """Caption ``job`` if Vimeo is ready, and return the caption state it lands in.

    Never raises. The replay is already published; nothing that happens here can
    be worth failing it over, and the outcome is recorded on the row either way.
    """
    ref = video_ref(job)
    if ref is None:  # pragma: no cover - `due` filters these out
        return _settle(db, job, SKIPPED, "The job has no Vimeo video to caption")

    targets = locales()
    if not targets:
        return _settle(db, job, SKIPPED, "No caption locales are configured")

    try:
        ready = has_source_track(ref)
    except Exception as exc:  # noqa: BLE001 — a Vimeo hiccup is worth another sweep
        logger.warning("Could not list caption tracks for %s: %s", ref, exc)
        return job.captions_state

    if not ready:
        job.captions_attempts += 1
        if job.captions_attempts >= settings.video_caption_max_attempts:
            logger.info(
                "Giving up on captions for job %s — Vimeo produced no %s transcript "
                "in %d checks",
                job.id,
                SOURCE_LANGUAGE,
                job.captions_attempts,
            )
            return _settle(
                db,
                job,
                SKIPPED,
                f"Vimeo produced no {SOURCE_LANGUAGE} transcript to translate",
            )
        db.commit()
        return PENDING

    job.captions_state = RUNNING
    job.captions_error = None
    db.commit()

    # Built rather than registered: the registry exists so the admin screen can
    # stream a run it started, and this run has nobody watching. Registering it
    # from the scheduler's thread would also touch a structure the API's event
    # loop owns.
    cc_job = VideoCcJob(id=uuid.uuid4().hex, video_ref=ref)
    logger.info("Captioning job %s (%s) into %s", job.id, ref, ", ".join(targets))
    try:
        asyncio.run(video_cc_service.run_job(cc_job, None, targets))
    except Exception as exc:  # noqa: BLE001 — run_job swallows its own, this is the loop
        logger.exception("Caption run crashed for job %s", job.id)
        return _settle(db, job, FAILED, f"Caption run crashed: {exc}")

    if cc_job.status == "completed":
        return _settle(db, job, COMPLETED)
    return _settle(db, job, FAILED, _last_error(cc_job) or "The caption run failed")


def _last_error(cc_job: VideoCcJob) -> str | None:
    """The most useful line of a failed run's event log."""
    for event in reversed(cc_job.events):
        if event.get("type") in ("error", "language_error") and event.get("error"):
            return str(event["error"])
    return None


__all__ = [
    "COMPLETED",
    "FAILED",
    "PENDING",
    "RUNNING",
    "SKIPPED",
    "due",
    "locales",
    "run",
    "video_ref",
]
