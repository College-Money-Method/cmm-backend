"""ECS task entrypoint: ``python -m src.video_pipeline.reel_task --reel-id <uuid>``.

Same image and task definition as ``run_task``, launched with a different
command by ``task_dispatch.dispatch_reel``. It downloads the job's archived
recordings, runs ``reel_build`` and uploads the finished mp4 to
``video-pipeline/reels/{job_id}/{reel_id}.mp4`` for the admin screen to preview.

Like ``run_task``, it turns every exception into a `failed` row with the
reason, so an admin never watches a reel that died silently. The barrel import
registers every mapper for the same reason ``run_task`` gives.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import update

import src.db.models  # noqa: F401 - registers every mapper, see run_task
from src.config import settings
from src.db.base import get_session_factory
from src.storage.s3_client import s3_client
from src.video_pipeline import reel_sources
from src.video_pipeline.ffmpeg_ops import require_ffmpeg
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_build import build_reel
from src.video_pipeline.reel_models import (
    ACTIVE_STATES,
    FAILED,
    READY,
    RENDERING,
    WebinarVideoReel,
)

logger = logging.getLogger(__name__)


def reel_key(job_id: uuid.UUID, reel_id: uuid.UUID) -> str:
    return f"video-pipeline/reels/{job_id}/{reel_id}.mp4"


def scratch_prefix(job_id: uuid.UUID) -> str:
    """Where the reel audio waits for Transcribe; the task may delete here."""
    return f"video-pipeline/reels/{job_id}/tmp"


def _set_stage(db, reel: WebinarVideoReel, stage: str) -> None:
    reel.stage = stage
    db.commit()
    logger.info("Reel %s: %s", reel.id, stage)


def _finish(db, reel_id: uuid.UUID, allowed: tuple[str, ...], **values) -> bool:
    """Write the reel's final state only while it is still in `allowed`.

    The list endpoint fails a reel that stops reporting for an hour, and the
    admin may then ask for another. A task that was merely slow must not turn
    that failed row back into ready, or write over it, once it does finish.
    """
    result = db.execute(
        update(WebinarVideoReel)
        .where(WebinarVideoReel.id == reel_id, WebinarVideoReel.state.in_(allowed))
        .values(**values)
    )
    db.commit()
    return result.rowcount == 1


def _render(db, reel: WebinarVideoReel, job: WebinarVideoJob, work: Path) -> None:
    inputs = reel_sources.load_inputs(job)
    _set_stage(db, reel, "downloading")
    camera = reel_sources.download_camera_video(job, work / "camera.mp4")
    screen = reel_sources.download_screen(job, work / "screen.mp4")
    built = build_reel(inputs, camera=camera, screen=screen, orientation=reel.orientation,
                       focus=reel.prompt, work_dir=work,
                       scratch_prefix=scratch_prefix(job.id),
                       on_stage=lambda stage: _set_stage(db, reel, stage))

    _set_stage(db, reel, "uploading")
    key = reel_key(job.id, reel.id)
    s3_client().upload_file(str(built.path), settings.s3_bucket_name, key,
                            ExtraArgs={"ContentType": "video/mp4"})
    if not _finish(db, reel.id, (RENDERING,), state=READY, stage=None, error=None,
                   s3_key=key, hook_title=built.hook_title,
                   duration_seconds=Decimal(str(built.duration_seconds))):
        logger.warning("Reel %s was failed while rendering — leaving it failed", reel.id)


def run(reel_id: str) -> int:
    """Render one reel. Returns a process exit code."""
    db = get_session_factory()()
    try:
        reel = db.get(WebinarVideoReel, uuid.UUID(reel_id))
        if reel is None:
            logger.error("No reel with id %s", reel_id)
            return 1
        if reel.state not in ACTIVE_STATES:
            logger.warning("Reel %s is %s, not runnable — exiting", reel.id, reel.state)
            return 0
        job = db.get(WebinarVideoJob, reel.job_id)
        reel.state = RENDERING
        db.commit()
        require_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="video-reel-") as tmp:
            _render(db, reel, job, Path(tmp))
        logger.info("Reel %s done", reel.id)
        return 0
    except Exception as exc:
        logger.exception("Reel %s failed: %s", reel_id, exc)
        try:
            db.rollback()
            _finish(db, uuid.UUID(reel_id), ACTIVE_STATES, state=FAILED, error=str(exc)[:2000])
        except Exception:
            logger.exception("Could not record the failure for reel %s", reel_id)
        return 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render one trailer reel")
    parser.add_argument("--reel-id", required=True, help="WebinarVideoReel UUID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL, and Transcribe's result URL is presigned.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run(args.reel_id)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
