"""In-process APScheduler lifecycle: registers the email automations job.

Business logic (querying due `PortalMapping` rows per enabled `EmailAutomation`,
resolving templates, sending to opted-in contacts) lives in
`src.emails.automation_runner` — this module only owns starting/stopping the
scheduler and wiring the job.
"""

from __future__ import annotations

from functools import lru_cache

from apscheduler.schedulers.background import BackgroundScheduler

from src.emails.automation_runner import run_automations_check

_JOB_ID = "email_automations_check"
_JOB_INTERVAL_MINUTES = 30


@lru_cache(maxsize=1)
def _get_scheduler() -> BackgroundScheduler:
    """Process-wide singleton scheduler instance. `lru_cache` guards against
    creating a second `BackgroundScheduler` if `init_scheduler` is ever called
    more than once in the same process (e.g. an accidental double-call from a
    reload hook)."""
    return BackgroundScheduler()


def init_scheduler(app) -> BackgroundScheduler:  # noqa: ARG001 - app kept for lifespan call-site symmetry
    """Start (once, process-wide) the in-process scheduler and register the
    email automations check on a 30-minute interval. Safe to call more than
    once: the job is registered with a fixed id + `replace_existing=True`.
    """
    scheduler = _get_scheduler()
    scheduler.add_job(
        run_automations_check,
        "interval",
        minutes=_JOB_INTERVAL_MINUTES,
        id=_JOB_ID,
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
    return scheduler


def shutdown_scheduler() -> None:
    scheduler = _get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
