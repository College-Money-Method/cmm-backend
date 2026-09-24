"""Launch the one-shot ECS processing task for a job, under a concurrency cap.

Only the ffmpeg stages run out of process. On the API's 1 vCPU a 90-minute
transcode would starve uvicorn for its whole duration, so download, archive,
trim, sample and upload are a Fargate task; chaptering and publish stay in the
API.

The transition to `processing` is made here, at dispatch, rather than by the
task once it boots. Fargate cold start is tens of seconds, and a job that has
been launched but not yet booted must already be visible to
``count_in_flight`` — otherwise the cap counts only warm tasks and the next
sweep over-dispatches into a burst.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.config import settings
from src.video_pipeline import job_service, stage_progress
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import FAILED, PENDING, RENDERING, WebinarVideoReel
from src.video_pipeline.states import JobState

logger = logging.getLogger(__name__)

# Entrypoints the task image runs, appended with ``--job-id`` / ``--reel-id``.
# Internal contracts with ``run_task.py`` and ``reel_task.py``, not
# environment-varying values, so they live here rather than in config.
_TASK_COMMAND: list[str] = ["python", "-m", "src.video_pipeline.run_task"]
_REEL_TASK_COMMAND: list[str] = ["python", "-m", "src.video_pipeline.reel_task"]


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def is_configured() -> bool:
    """True when every value RunTask needs is present.

    Unconfigured is a normal local-dev state, not an error: jobs are still
    created and still listed on the admin screen, they simply never launch.
    """
    return bool(
        settings.ecs_cluster_arn
        and settings.video_task_definition_arn
        and _split_csv(settings.video_task_subnets)
    )


def _run_task(job_id: str) -> str | None:
    """Launch the processing task for one job; see ``_launch``."""
    return _launch([*_TASK_COMMAND, "--job-id", job_id])


def _run_reel_task(reel_id: str) -> str | None:
    """Launch the reel task for one reel; see ``_launch``."""
    return _launch([*_REEL_TASK_COMMAND, "--reel-id", reel_id])


def _launch(command: list[str]) -> str | None:
    """Call ECS RunTask with `command` and return the task ARN, or None if ECS
    accepted nothing."""
    import boto3  # local import: keeps boto3 out of the import path of every test

    client = boto3.client("ecs", region_name=settings.aws_region)
    network: dict[str, object] = {
        "subnets": _split_csv(settings.video_task_subnets),
        # Public IP is required for egress on a public subnet with no NAT
        # gateway; on a private subnet with NAT it is ignored by the caller's
        # routing, so ENABLED is the safe default for both layouts.
        "assignPublicIp": "ENABLED",
    }
    if settings.video_task_security_group:
        network["securityGroups"] = _split_csv(settings.video_task_security_group)

    response = client.run_task(
        cluster=settings.ecs_cluster_arn,
        taskDefinition=settings.video_task_definition_arn,
        launchType="FARGATE",
        count=1,
        networkConfiguration={"awsvpcConfiguration": network},
        overrides={
            "containerOverrides": [
                {
                    "name": settings.video_task_container_name,
                    "command": command,
                }
            ]
        },
    )

    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(f"ECS RunTask reported failures: {failures}")

    tasks = response.get("tasks") or []
    return tasks[0].get("taskArn") if tasks else None


def dispatch(db: Session, job: WebinarVideoJob) -> bool:
    """Try to launch ``job``. Returns True if it moved to `processing`.

    Returns False without raising in every case where launching is not possible
    right now — over the cap, not configured, or ECS refused. The job stays
    `pending` and the sweeper retries it, because all three are conditions that
    clear on their own or are visible on the admin screen.
    """
    if job.job_state is not JobState.PENDING:
        logger.warning("Video job not pending — skipping dispatch job=%s state=%s", job.id, job.state)
        return False

    if not is_configured():
        logger.warning(
            "ECS video task not configured — job=%s left pending (set ECS_CLUSTER_ARN, "
            "VIDEO_TASK_DEFINITION_ARN, VIDEO_TASK_SUBNETS)",
            job.id,
        )
        return False

    in_flight = job_service.count_in_flight(db)
    if in_flight >= settings.video_pipeline_max_concurrent:
        logger.info(
            "Video pipeline at capacity — job=%s stays pending (in_flight=%d cap=%d)",
            job.id,
            in_flight,
            settings.video_pipeline_max_concurrent,
        )
        return False

    try:
        task_arn = _run_task(str(job.id))
    except Exception as exc:
        # Transient (capacity, throttling) or permanent (bad ARN) — both leave
        # the job pending. A permanently undispatchable job shows up on the
        # admin screen rather than burning retries against a failed state.
        logger.error("ECS RunTask failed — job=%s error=%s", job.id, exc)
        return False

    job_service.advance(db, job, JobState.PROCESSING, ecs_task_arn=task_arn)
    # Recorded here rather than by the task: the container takes up to a minute
    # to boot, and that wait is exactly what an operator watching a job that has
    # "started" but shows nothing yet needs to see accounted for.
    stage_progress.record(db, job, stage_progress.STARTING_TASK)
    logger.info("Video job dispatched — job=%s task=%s", job.id, task_arn)
    return True


def dispatch_reel(db: Session, reel: WebinarVideoReel) -> bool:
    """Try to launch the reel task. Returns True if the reel moved to `rendering`.

    Reels sit outside the processing cap: an admin asks for one at a time per
    job, and the cap exists to keep a backlog of webhook-driven jobs in check.
    Unconfigured (local dev) leaves the reel `pending`, to be run by hand with
    ``python -m src.video_pipeline.reel_task --reel-id <uuid>``; a launch ECS
    refuses fails the reel with the reason.
    """
    if reel.state != PENDING:
        logger.warning("Reel not pending — skipping dispatch reel=%s state=%s", reel.id, reel.state)
        return False
    if not is_configured():
        logger.warning("ECS video task not configured — reel=%s left pending", reel.id)
        return False
    try:
        task_arn = _run_reel_task(str(reel.id))
    except Exception as exc:
        # Nothing retries a reel, so a refused launch is recorded as failed
        # rather than left pending, where it would block the next request.
        logger.error("ECS RunTask failed — reel=%s error=%s", reel.id, exc)
        reel.state, reel.error = FAILED, f"Could not start the render task — {exc}"
        db.commit()
        return False
    reel.state, reel.stage, reel.ecs_task_arn = RENDERING, "starting", task_arn
    db.commit()
    logger.info("Reel dispatched — reel=%s task=%s", reel.id, task_arn)
    return True
