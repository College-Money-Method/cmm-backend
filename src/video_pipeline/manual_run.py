"""Start one audit run from a pasted source.

An audit run is a deliberate operator action, not a webhook: someone pastes a
Zoom reference or a download URL and gets a processed video in the Vimeo audit
folder to watch. It deliberately stops there — it never writes
``webinars.video_embed_code`` and never deletes the Zoom recording, so running
one on a live session cannot change what any school sees or destroy the source.

Everything that can be checked cheaply is checked here rather than inside the
ECS task: a typo, an unreachable host or an unconfigured audit folder should
come back as an error on the form, not as a `failed` job ten minutes later.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.integrations import zoom
from src.integrations.vimeo_upload import audit_folder_uri
from src.video_pipeline import job_service, task_dispatch, url_recording_fetch
from src.video_pipeline.manual_source import parse_source
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

_URL_KEY_PREFIX = "manual:"


class RunError(RuntimeError):
    """The run cannot be started, and the operator can fix the reason."""


class RunConflict(RuntimeError):
    """A job already exists for this recording."""

    def __init__(self, message: str, job: WebinarVideoJob) -> None:
        super().__init__(message)
        self.job = job


@dataclass(frozen=True)
class StartedRun:
    job: WebinarVideoJob
    dispatched: bool


def _require_audit_destination() -> None:
    """Refuse before touching Zoom if there is nowhere safe to put the result.

    Checked up front because the alternative surfaces much later: the source is
    downloaded, trimmed and sampled, and only the upload discovers it has no
    folder to go in. Failing here costs the operator a second.
    """
    if not audit_folder_uri():
        raise RunError(
            "No audit folder is configured, so there is nowhere to upload into. "
            "Set it in Global Settings (or VIMEO_AUDIT_FOLDER_URI) to the folder's "
            "URI, /users/<id>/projects/<id>."
        )


def _validated_transcript_url(raw: str | None) -> str | None:
    """Check an optional transcript URL now, or return None if none was given.

    Blank is "not supplied", not "supplied empty": the field is optional and an
    operator who clears it means the same as one who never filled it. A URL that
    is given is held to the same host rules as the source, here rather than in
    the task, so a typo comes back on the form.
    """
    value = (raw or "").strip()
    if not value:
        return None
    url_recording_fetch.assert_public_url(value)
    return value


def _resolve_zoom_recording(reference: str) -> str:
    """Turn a meeting ID or recording UUID into the recording's own UUID.

    Zoom's ``/meetings/{id}/recordings`` accepts either, and answers a meeting
    ID with that meeting's most recent instance. Taking ``uuid`` from the
    response is what lets a pasted meeting ID share the same idempotency key as
    the webhook for the same recording, instead of creating a second job for it.
    """
    payload = zoom.get_recording(reference)
    if payload is None:
        raise RunError(
            f"Zoom returned no recording for '{reference}'. Check the ID, that the "
            "recording finished processing, and that it has not been deleted."
        )
    recording_uuid = str(payload.get("uuid") or "").strip()
    if not recording_uuid:
        raise RunError(f"Zoom's recording for '{reference}' carries no UUID to key a job on")
    return recording_uuid


def start(
    db: Session,
    *,
    source: str,
    webinar_id: uuid.UUID | None = None,
    transcript_url: str | None = None,
) -> StartedRun:
    """Create an audit job for ``source`` and dispatch it.

    ``webinar_id`` only names the Vimeo video. An audit run never publishes to
    the webinar it names, so attaching one is a convenience for whoever reviews
    the folder, not a commitment to anything.

    ``transcript_url`` is optional and worth supplying: without a transcript the
    trim comes from silence detection and the chapters from frames alone, so an
    audit run on a pasted URL would otherwise exercise a different, degraded
    path from the one a real webinar takes. For a Zoom source it overrides
    Zoom's own transcript, which is how a recording with transcription switched
    off can still be chaptered properly.

    Raises:
        SourceError: the paste is not a source this can act on.
        UrlFetchError: the URL's host is not one we will fetch from.
        RunError: the run cannot start for a reason the operator can fix.
        RunConflict: this recording already has a job.
    """
    _require_audit_destination()
    parsed = parse_source(source)
    transcript = _validated_transcript_url(transcript_url)

    if parsed.is_zoom:
        recording_uuid = _resolve_zoom_recording(parsed.zoom_reference)
        existing = job_service.get_by_recording_uuid(db, recording_uuid)
        if existing is not None:
            raise RunConflict(
                f"Recording {recording_uuid} already has job {existing.id} "
                f"({existing.state}). Retry that job instead of starting a second run "
                "on the same recording.",
                existing,
            )
        source_url = None
    else:
        # Rejects a private or link-local host now, while the message can still
        # reach the person who pasted it.
        url_recording_fetch.assert_public_url(parsed.url)
        # A download URL has no stable identity — the same file can arrive under
        # a fresh presigned URL every time — so nothing here can be deduplicated
        # against. The UNIQUE key still has to be filled, and a random one keeps
        # the constraint honest instead of relaxing it.
        recording_uuid = f"{_URL_KEY_PREFIX}{uuid.uuid4()}"
        source_url = parsed.url

    job, created = job_service.create_from_recording(
        db,
        zoom_recording_uuid=recording_uuid,
        webinar_id=webinar_id,
        source_url=source_url,
        transcript_url=transcript,
        audit_only=True,
    )
    if not created:  # pragma: no cover - the checks above already returned
        raise RunConflict(f"Recording {recording_uuid} already has job {job.id}", job)

    dispatched = task_dispatch.dispatch(db, job)
    logger.info(
        "Audit run started — job=%s source=%s transcript=%s dispatched=%s",
        job.id,
        source_url or recording_uuid,
        transcript or "none",
        dispatched,
    )
    return StartedRun(job=job, dispatched=dispatched)
