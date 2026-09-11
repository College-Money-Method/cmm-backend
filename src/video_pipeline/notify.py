"""Ops alerts for a video job — one when it fails, one when it publishes.

The pipeline is unattended, so nobody is watching a run when it breaks. Email
is the push channel; the phase-04 admin screen is the pull channel that does
not depend on email surviving suppression or a bounce.

Two rules shape everything here:

1. **Never raise.** This is called from ``job_service.fail`` immediately after
   the failure has been recorded. An exception escaping would roll the alert
   attempt into the failure path and could lose the record of *why* the job
   failed, which is strictly worse than a missing email.
2. **Once per failure, not once per sweep.** The sweeper re-reads failed rows
   every few minutes. ``failed_notified_at`` is the stamp that separates a new
   failure from a re-read; ``job_service.retry`` clears it, so a job that fails
   again does alert again.

The success alert needs no such stamp: publishing is what moves a job out of
`chaptering`, and `chaptering` is the only state the publisher reads, so the
step that sends it runs once per job by construction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.config import settings
from src.emails.ses_client import send_email
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

_SOURCE = "video_pipeline"


def _webinar_label(job: WebinarVideoJob) -> str:
    """Human name for the webinar, falling back to ids when the join is unusable."""
    try:
        webinar = job.webinar
        if webinar is not None:
            name = getattr(webinar, "webinar_name", None)
            zoom_id = getattr(webinar, "zoom_webinar_id", None)
            if name and zoom_id:
                return f"{name} (Zoom {zoom_id})"
            if name:
                return str(name)
    except Exception:  # pragma: no cover - detached instance / missing row
        pass
    if job.webinar_id is None:
        # An audit run started from a paste has no webinar at all. Naming the
        # source is what tells the reader which run failed.
        return f"audit run of {job.source_url or job.zoom_recording_uuid}"
    return f"webinar {job.webinar_id}"


def _render(job: WebinarVideoJob) -> tuple[str, str, str]:
    """Subject, HTML body, plain-text body for the failure alert."""
    label = _webinar_label(job)
    subject = f"[Video pipeline] Job failed — {label}"
    rows = [
        ("Webinar", label),
        ("Job", str(job.id)),
        ("Attempt", str(job.attempt + 1)),
        ("Zoom recording", job.zoom_recording_uuid),
        ("ECS task", job.ecs_task_arn or "not dispatched"),
        ("Error", job.error or "(no detail recorded)"),
    ]
    text = "\n".join(f"{name}: {value}" for name, value in rows)
    body = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'><strong>{name}</strong></td>"
        f"<td style='padding:4px 0'>{value}</td></tr>"
        for name, value in rows
    )
    html = (
        "<p>A webinar video job failed. The webinar's replay was left untouched — "
        "no broken embed was published.</p>"
        f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>{body}</table>"
        "<p>Retry it from the video pipeline screen in the admin hub. The Zoom cloud "
        "recording is still in the storage pool until the job completes.</p>"
    )
    return subject, html, text


def _render_published(job: WebinarVideoJob, title: str) -> tuple[str, str, str]:
    """Subject, HTML body, plain-text body for the "replay is ready" alert."""
    subject = f"[Video pipeline] Ready — {title}"
    rows = [
        ("Video", title),
        ("Webinar", _webinar_label(job)),
        ("Watch", f"https://vimeo.com/{job.vimeo_video_id}" if job.vimeo_video_id else "—"),
        ("Chapters", str(len(job.chapters or []))),
        ("Job", str(job.id)),
    ]
    text = "\n".join(f"{name}: {value}" for name, value in rows)
    body = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'><strong>{name}</strong></td>"
        f"<td style='padding:4px 0'>{value}</td></tr>"
        for name, value in rows
    )
    html = (
        "<p>A webinar replay finished processing and is now embedded on its "
        "webinar page.</p>"
        f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>{body}</table>"
        "<p>Translated captions are added separately, once Vimeo has written the "
        "English transcript.</p>"
    )
    return subject, html, text


def notify_published(db: Session, job: WebinarVideoJob, title: str) -> bool:
    """Tell ops the replay is live. Returns True if an email went out.

    Never raises, for the same reason the failure alert does not: the replay is
    published and the embed code is written by the time this runs, and losing
    that record to a mail server having a bad minute would be far worse than a
    missing notification.

    Audit runs are silent. An audit publishes nothing a school can see, so there
    is nothing for anyone to be told about.
    """
    try:
        if job.audit_only:
            return False
        if not settings.video_pipeline_alert_email:
            logger.info("VIDEO_PIPELINE_ALERT_EMAIL is unset — no ready alert for job %s", job.id)
            return False

        subject, html, text = _render_published(job, title)
        with db.begin_nested():
            send_email(
                db,
                to=settings.video_pipeline_alert_email,
                subject=subject,
                html=html,
                text=text,
                source=_SOURCE,
                webinar_id=job.webinar_id,
            )
        db.commit()
        logger.info("Video job ready alert sent — job=%s", job.id)
        return True

    except Exception as exc:
        logger.exception("Video job ready alert could not be sent — job=%s error=%s", job.id, exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - session already unusable
            pass
        return False


def notify_failure(db: Session, job: WebinarVideoJob) -> bool:
    """Send the one alert this failure is owed. Returns True if an email went out.

    Never raises. The send runs inside a SAVEPOINT so that a failure writing the
    ``email_send_log`` row cannot poison the caller's transaction.
    """
    try:
        if job.failed_notified_at is not None:
            return False

        if not settings.video_pipeline_alert_email:
            logger.error(
                "Video job failed and VIDEO_PIPELINE_ALERT_EMAIL is unset — no alert sent. "
                "job=%s webinar=%s error=%s",
                job.id,
                job.webinar_id,
                job.error,
            )
            return False

        subject, html, text = _render(job)
        with db.begin_nested():
            send_email(
                db,
                to=settings.video_pipeline_alert_email,
                subject=subject,
                html=html,
                text=text,
                source=_SOURCE,
                webinar_id=job.webinar_id,
            )

        job.failed_notified_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Video job failure alert sent — job=%s", job.id)
        return True

    except Exception as exc:
        logger.exception("Video job failure alert could not be sent — job=%s error=%s", job.id, exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - session already unusable
            pass
        return False
