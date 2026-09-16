"""ECS task entrypoint: ``python -m src.video_pipeline.run_task --job-id <uuid>``.

Thin on purpose. It owns process concerns — argument parsing, logging, the DB
session, the scratch directory, and turning any exception into a `failed` job
row — while ``process_recording.process`` owns the pipeline itself.

The barrel import is not decorative. ``WebinarVideoJob.webinar`` is a string
relationship to ``Webinar``, which SQLAlchemy resolves out of the mapper
registry on first use. The API process fills that registry as a side effect of
importing its routers; a bare ``python -m`` does not, so without the barrel the
very first ORM query raises "failed to locate a name ('Webinar')" and the task
dies before it can even read the job it was launched for.

The fail-safe wrapper is the point. A task that exits on an unhandled exception
leaves a job stuck in `processing` until the sweeper times it out 90 minutes
later, with no reason recorded anywhere an admin can see. Catching here means
every ending is written down.

One ending is not a failure. Zoom fires ``recording.completed`` when it has
finished recording, which can be minutes ahead of the files being fetchable, so
a job dispatched straight off the webhook regularly arrives before its source
does. That run is put back in the queue instead of failed — see ``_wait_again``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

import src.db.models  # noqa: F401 - see below
from src.db.base import get_session_factory
from src.video_pipeline import job_service, process_recording
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.video_pipeline.zoom_recording_fetch import RecordingNotReadyError

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _claim(db, job: WebinarVideoJob) -> bool:
    """Make sure the job is ours to run.

    The dispatcher already moved the job to `processing` before RunTask, so that
    is the expected state. `pending` is accepted too — a task launched by hand
    for a job the dispatcher never claimed — and everything else is a no-op:
    re-running a published job must not redo the work.
    """
    if job.job_state is JobState.PROCESSING:
        return True
    if job.job_state is JobState.PENDING:
        job_service.advance(db, job, JobState.PROCESSING)
        return True
    logger.warning("Job %s is %s, not runnable — exiting without work", job.id, job.state)
    return False


# How many times a job may be put back for a source that is not ready yet. The
# sweeper dispatches `pending` every five minutes, so this is roughly an hour of
# waiting — well past Zoom's own "a few minutes", and short of the point where
# something other than transcoding is wrong and ops should hear about it.
_MAX_SOURCE_WAITS = 12


def _wait_again(db, job: WebinarVideoJob, exc: RecordingNotReadyError) -> bool:
    """Put ``job`` back in the queue, unless it has waited long enough already.

    Returns True when the job was requeued, False when the caller should treat
    this as a failure like any other.
    """
    if job.attempt >= _MAX_SOURCE_WAITS:
        logger.warning(
            "Job %s has waited %d times for Zoom to finish processing — failing",
            job.id,
            job.attempt,
        )
        return False
    job_service.requeue(db, job, f"Waiting for Zoom to finish processing the recording — {exc}")
    return True


def run(job_id: str) -> int:
    """Process one job. Returns a process exit code."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        job = db.get(WebinarVideoJob, uuid.UUID(job_id))
        if job is None:
            logger.error("No video job with id %s", job_id)
            return 1
        if not _claim(db, job):
            return 0

        with tempfile.TemporaryDirectory(prefix="video-pipeline-") as tmp:
            process_recording.process(db, job, Path(tmp))
        logger.info("Job %s finished processing", job.id)
        return 0

    except RecordingNotReadyError as exc:
        # Not a failure and not worth a traceback: the run was simply early.
        logger.info("Job %s cannot start yet — %s", job_id, exc)
        try:
            job = db.get(WebinarVideoJob, uuid.UUID(job_id))
            if job is not None and job.job_state in (JobState.PROCESSING, JobState.PENDING):
                db.rollback()
                if _wait_again(db, job, exc):
                    return 0
                job_service.fail(db, job, str(exc))
        except Exception:
            logger.exception("Could not requeue job %s", job_id)
        return 1

    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        try:
            job = db.get(WebinarVideoJob, uuid.UUID(job_id))
            if job is not None and job.job_state in (JobState.PROCESSING, JobState.PENDING):
                db.rollback()
                job_service.fail(db, job, f"{exc}\n{traceback.format_exc()[-2000:]}")
        except Exception:
            # Recording the failure failed too (DB gone). The sweeper's
            # processing timeout is the backstop for exactly this.
            logger.exception("Could not record the failure for job %s", job_id)
        return 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process one webinar video job")
    parser.add_argument("--job-id", required=True, help="WebinarVideoJob UUID")
    args = parser.parse_args(argv)

    _configure_logging()
    return run(args.job_id)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
