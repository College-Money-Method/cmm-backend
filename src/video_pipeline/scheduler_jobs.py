"""Registers the pipeline's background jobs on the app's shared scheduler.

Kept here rather than in ``emails.scheduler`` so the email package does not
grow an import of the video pipeline. That module owns the scheduler's
lifecycle and hands the instance back; this one only adds jobs to it.
"""

from __future__ import annotations

import logging

from src.video_pipeline.reconcile import reconcile_recordings
from src.video_pipeline.sweeper import run_video_pipeline_sweep

logger = logging.getLogger(__name__)

_SWEEP_JOB_ID = "video_pipeline_sweep"
# Short: the sweep is what drains the backlog after a burst, and the target is
# Zoom cloud storage back to baseline within ~30 minutes of a webinar ending.
_SWEEP_INTERVAL_MINUTES = 5

_RECONCILE_JOB_ID = "video_pipeline_reconcile"
# Hourly is well inside Zoom's 7-day auto-delete backstop, and each run costs
# one paginated API call.
_RECONCILE_INTERVAL_MINUTES = 60


def register_video_pipeline_jobs(scheduler) -> None:
    """Add the sweep and reconcile jobs. Idempotent — fixed ids, replace_existing."""
    scheduler.add_job(
        run_video_pipeline_sweep,
        "interval",
        minutes=_SWEEP_INTERVAL_MINUTES,
        id=_SWEEP_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        reconcile_recordings,
        "interval",
        minutes=_RECONCILE_INTERVAL_MINUTES,
        id=_RECONCILE_JOB_ID,
        replace_existing=True,
    )
    logger.info(
        "Video pipeline jobs registered — sweep=%dm reconcile=%dm",
        _SWEEP_INTERVAL_MINUTES,
        _RECONCILE_INTERVAL_MINUTES,
    )
