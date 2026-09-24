"""What a trailer reel is cut from: a published job's transcript, chapters and recordings.

Everything comes from what the pipeline kept, not from Vimeo:

* **Cues**, on the trimmed clock the chapters use. The job's ``transcript.json``
  first (Zoom's or the operator's transcript, re-based at processing time); then
  the archived raw ``source.vtt`` re-based here by the trim offset; Vimeo's own
  track (of the trimmed upload, so already on that clock) only as a last resort.
  Word timings for the captions come from transcribing the reel itself, so the
  transcript only has to be good enough to choose sentences from.
* **The shared screen**: the archived untrimmed original. Vimeo's copy is
  trimmed, re-encoded and embed-only.
* **The camera**: Zoom's camera-only rendition, archived next to the original
  since reels existed; for an older job, Zoom's copy while it still has one.
  Zoom deletes its copy when the replay is published, so an older published
  job has no camera shot to cut to and cannot have a reel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.integrations import vimeo, zoom
from src.video_pipeline import archive_original, artifact_store, job_views
from src.video_pipeline.archive_original import CAMERA_FILENAME, TRANSCRIPT_FILENAME, VIDEO_FILENAME
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.video_pipeline.transcript import Cue, VttError, parse_cues, rebase
from src.video_pipeline.zoom_recording_fetch import download_camera, select_camera_file

logger = logging.getLogger(__name__)


class ReelSourceError(RuntimeError):
    """Something a reel needs is not there."""


@dataclass(frozen=True)
class ReelInputs:
    title: str
    cues: list[Cue]
    chapters: list[dict[str, Any]]
    trim_offset: float


def _zoom_has_camera(job: WebinarVideoJob) -> bool:
    """Whether Zoom still has a camera-only rendition of the job's recording.

    Publishing deletes Zoom's copy, but a delete that failed leaves it there
    until Zoom's own auto-delete, so this asks rather than assumes.
    """
    try:
        payload = zoom.get_recording(job.zoom_recording_uuid)
    except zoom.ZoomApiError as exc:
        logger.info("Zoom has no recording %s — %s", job.zoom_recording_uuid, exc)
        return False
    return payload is not None and select_camera_file(payload) is not None


def blocked_reason(job: WebinarVideoJob) -> str | None:
    """Why no reel can be made of `job`, or None when one can.

    Two S3 HEADs, plus one Zoom lookup for a job processed before camera
    recordings were archived: cheap enough for every load of the admin screen.
    """
    if job.job_state is not JobState.PUBLISHED:
        return "Reels can be made once the job is published."
    if job.trim_offset_seconds is None:
        return "The job has no trim offset."
    if (job_views.archive_expired(job) or not job.archive_key
            or not archive_original.archived_original_exists(job.archive_key)):
        return "The archived recording has expired — nothing left to cut from."
    if archive_original.archived_original_exists(job.archive_key, CAMERA_FILENAME):
        return None
    if job.source_url:
        return "A download-URL source has no separate camera recording to cut to."
    if _zoom_has_camera(job):
        return None  # the reel task downloads it from Zoom
    return ("This recording was processed before camera recordings were archived, and "
            "Zoom no longer has its camera recording.")


def load_inputs(job: WebinarVideoJob) -> ReelInputs:
    """Title, cues, chapters and trim offset. Raises ReelSourceError without cues."""
    if job.trim_offset_seconds is None:
        raise ReelSourceError(f"Job {job.id} has no trim offset")
    offset = float(job.trim_offset_seconds)
    cues = _saved_cues(job) or _archived_cues(job, offset) or _vimeo_cues(job)
    if not cues:
        raise ReelSourceError("No transcript for this recording — nothing to pick clips from")
    title = job.webinar.webinar_name if job.webinar else ""
    return ReelInputs(title=title or "", cues=cues, chapters=job.chapters or [],
                      trim_offset=offset)


def download_screen(job: WebinarVideoJob, dest: Path) -> Path:
    """The archived shared-screen original (Glacier Instant Retrieval: a plain GET)."""
    try:
        return archive_original.download_archived(job.archive_key or "", VIDEO_FILENAME, dest)
    except archive_original.ArchiveError as exc:
        raise ReelSourceError(str(exc)) from exc


def download_camera_video(job: WebinarVideoJob, dest: Path) -> Path:
    """The camera-only rendition: from the archive, else from Zoom while it has it."""
    if job.archive_key and archive_original.archived_original_exists(job.archive_key,
                                                                      CAMERA_FILENAME):
        try:
            return archive_original.download_archived(job.archive_key, CAMERA_FILENAME, dest)
        except archive_original.ArchiveError as exc:
            raise ReelSourceError(str(exc)) from exc
    if job.source_url:
        raise ReelSourceError("A download-URL source has no camera recording")
    try:
        payload = zoom.get_recording(job.zoom_recording_uuid)
    except zoom.ZoomApiError as exc:
        raise ReelSourceError(f"Zoom no longer has the camera recording — {exc}") from exc
    if payload is None:
        raise ReelSourceError("Zoom credentials are not configured")
    camera = download_camera(payload, zoom.recording_access_token(), dest)
    if camera is None:
        raise ReelSourceError("Zoom has no camera-only rendition of this recording")
    return camera


def _saved_cues(job: WebinarVideoJob) -> list[Cue]:
    if not job.frames_prefix:
        return []
    try:
        raw = artifact_store.load_json_artifact(job.frames_prefix,
                                                artifact_store.TRANSCRIPT_FILENAME)
    except artifact_store.ArtifactError as exc:
        logger.info("No saved transcript for job %s — %s", job.id, exc)
        return []
    return [Cue(**cue) for cue in raw] if isinstance(raw, list) else []


def _archived_cues(job: WebinarVideoJob, offset: float) -> list[Cue]:
    if not job.archive_key:
        return []
    try:
        content = archive_original.read_archived_text(job.archive_key, TRANSCRIPT_FILENAME)
        return rebase(parse_cues(content), offset)
    except (archive_original.ArchiveError, VttError) as exc:
        logger.info("No archived transcript for job %s — %s", job.id, exc)
        return []


def _vimeo_cues(job: WebinarVideoJob) -> list[Cue]:
    if not job.vimeo_video_id:
        return []
    ref = f"{job.vimeo_video_id}:{job.vimeo_hash}" if job.vimeo_hash else job.vimeo_video_id
    try:
        content, _name = vimeo.download_source_track(ref, "en")
        return parse_cues(content)
    except (vimeo.VimeoError, VttError) as exc:
        logger.info("No Vimeo transcript for job %s — %s", job.id, exc)
        return []
