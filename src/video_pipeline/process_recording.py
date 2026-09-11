"""The processing sequence one ECS task runs for one job.

Ordering here carries most of the design:

* the archive is written **before** any processing, because its job is to
  survive a failure;
* Vimeo ids are persisted **before** the transcode wait, so a task killed
  mid-poll does not upload the video a second time on retry;
* the Zoom recording is deleted **after** the transcode confirms, and a failed
  delete only logs — the video is already published by then, and Zoom's 7-day
  auto-delete is the backstop.

Every step raises on failure; ``run_task`` turns that into ``failed`` with the
message on the job row.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from src.config import settings
from src.integrations import zoom
from src.integrations.vimeo import VimeoError
from src.integrations.vimeo_upload import audit_folder_uri, create_video, wait_for_transcode
from src.video_pipeline import (
    archive_original,
    artifact_store,
    ffmpeg_ops,
    job_service,
    stage_progress,
    thumbnail,
    transcript,
    url_recording_fetch,
)
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.video_pipeline.trim_point_detect import detect_trim_point
from src.video_pipeline.video_title import video_title
from src.video_pipeline.zoom_recording_fetch import fetch_recording

logger = logging.getLogger(__name__)


class SamplingError(RuntimeError):
    """The sampling pass produced a candidate set the pipeline refuses to use."""


def _download_source(job: WebinarVideoJob, work_dir: Path) -> tuple[Path, Path | None, int]:
    """Fetch the source from wherever this job's descriptor points.

    A URL source reports no duration — it is probed from the file after
    trimming — and carries no transcript of its own, so it has one only if the
    operator supplied a URL for it. An operator-supplied transcript wins over
    Zoom's, which is how a recording whose account had transcription switched
    off gets chaptered from speech rather than from frames alone.
    """
    operator_transcript = (
        url_recording_fetch.fetch_transcript_from_url(
            job.transcript_url, work_dir / "operator-transcript.vtt"
        )
        if job.transcript_url
        else None
    )

    if job.source_url:
        video = url_recording_fetch.fetch_from_url(job.source_url, work_dir / "source.mp4")
        return video, operator_transcript, 0

    fetched = fetch_recording(job.zoom_recording_uuid, work_dir)
    return (
        fetched.video_path,
        operator_transcript or fetched.transcript_path,
        fetched.duration_seconds,
    )


def _acquire_source(db: Session, job: WebinarVideoJob, work_dir: Path) -> tuple[Path, Path | None]:
    """Get the untrimmed source, from the S3 archive when there is one.

    A retry days later will usually find Zoom's copy gone — or a pasted
    presigned URL expired — so the archive is preferred whenever the key is on
    the row and the object still exists. A first run falls through to the
    original source and archives what it downloads.
    """
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)
    if job.archive_key and archive_original.archived_original_exists(job.archive_key):
        logger.info("Re-running job %s from the S3 archive", job.id)
        return archive_original.restore_original(job.archive_key, work_dir)

    video_path, transcript_path, duration_seconds = _download_source(job, work_dir)
    stage_progress.record(db, job, stage_progress.ARCHIVING_SOURCE)
    prefix = archive_original.archive_original(str(job.id), video_path, transcript_path)
    job.archive_key = prefix
    job.archive_expires_at = archive_original.expires_at()
    if duration_seconds:
        job.source_duration_seconds = duration_seconds
    db.commit()
    return video_path, transcript_path


def _resolve_trim(video: Path, vtt: Path | None) -> tuple[float, list[transcript.Cue], bool]:
    """Decide where to cut. Returns ``(offset, raw_cues, fallback_used)``.

    With a transcript the choice is semantic — the presenter talks through the
    dead opening, so only meaning separates stalling from starting. Without one,
    all that is left is leading silence, which is the degraded path and is
    logged as such.
    """
    if vtt is None:
        offset = ffmpeg_ops.detect_speech_start(video)
        logger.warning("No transcript — trimming %.2fs from silence detection alone", offset)
        return offset, [], True

    cues = transcript.load_cues(vtt)
    point = detect_trim_point(cues)
    return point.offset, cues, point.fallback


def _upload_folder(job: WebinarVideoJob) -> str | None:
    """Vimeo folder this job's video belongs in, if any.

    An audit run with no folder configured refuses to upload. Failing closed is
    the point: the alternative is unreviewed videos accumulating in the same
    library the production replays live in, where nothing distinguishes them.
    """
    if not job.audit_only:
        return None
    folder = audit_folder_uri()
    if not folder:
        raise VimeoError(
            "This is an audit run but no audit folder is configured. Refusing to "
            "upload into the main library — set the folder's URI, "
            "/users/<id>/projects/<id>, in Global Settings (or "
            "VIMEO_AUDIT_FOLDER_URI)."
        )
    return folder


def _publish_to_vimeo(db: Session, job: WebinarVideoJob, trimmed: Path, title: str) -> str:
    """Upload unless this job already has a video, and return the video ref.

    The already-uploaded check is what makes the whole task safe to re-run: a
    task killed after the upload but before ``chaptering`` would otherwise
    publish the same webinar twice.
    """
    if job.vimeo_video_id:
        logger.info("Job %s already has Vimeo video %s — skipping upload", job.id, job.vimeo_video_id)
        return f"{job.vimeo_video_id}:{job.vimeo_hash}" if job.vimeo_hash else job.vimeo_video_id

    stage_progress.record(db, job, stage_progress.UPLOADING_TO_VIMEO)
    result = create_video(trimmed, title, folder_uri=_upload_folder(job))
    job.vimeo_video_id = result["video_id"]
    job.vimeo_hash = result["hash"] or None
    job.vimeo_player_embed_url = result["player_embed_url"] or None
    db.commit()
    return result["video_ref"]


def _delete_zoom_copy(db: Session, job: WebinarVideoJob) -> None:
    """Free the Zoom cloud pool. Never fatal — the replay is already published.

    Skipped for an audit run and for a URL source. An audit run must leave the
    account exactly as it found it: the recording it just read may still be
    waiting for its real production run, and deleting it would destroy the
    source to prove the pipeline works on it.
    """
    if job.audit_only or job.source_url:
        logger.info("Audit run %s — leaving the source recording in place", job.id)
        return
    stage_progress.record(db, job, stage_progress.DELETING_ZOOM_COPY)
    if not zoom.delete_recording(job.zoom_recording_uuid):
        logger.warning(
            "Zoom recording %s was not deleted — leaving it to the 7-day auto-delete",
            job.zoom_recording_uuid,
        )


def _guard_candidate_count(job: WebinarVideoJob, count: int) -> None:
    """Stop the job when sampling produced more candidates than we will pay for.

    Everything downstream of sampling is per-frame: an S3 PUT, an S3 GET, and a
    Bedrock vision call each. A filter that stops discriminating therefore does
    not degrade the output, it multiplies the bill — an earlier run of an
    82-minute recording sampled 2,655 states, spent 53 minutes moving them
    through S3, and classified 90% of them as the same speaker's face.

    Fails closed on purpose. A count this far above the design target means the
    filter did not recognise this recording's layout, so the chapters it would
    produce cannot be trusted either; better a `failed` row naming the number
    than a published video chaptered off noise.
    """
    limit = settings.video_max_sample_frames
    if count <= limit:
        return
    raise SamplingError(
        f"Sampling produced {count} candidate frames, above the {limit} ceiling — "
        "refusing to classify. The overlay crop or the scene threshold does not suit "
        f"this recording's layout; check the sampled frames for job {job.id}."
    )


def process(db: Session, job: WebinarVideoJob, work_dir: Path) -> None:
    """Run one job from source download through to ``chaptering``."""
    ffmpeg_ops.require_ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)

    source, vtt = _acquire_source(db, job, work_dir)

    stage_progress.record(db, job, stage_progress.DETECTING_TRIM)
    offset, cues, fallback = _resolve_trim(source, vtt)

    stage_progress.record(db, job, stage_progress.TRIMMING)
    trimmed = ffmpeg_ops.trim_stream_copy(source, work_dir / "trimmed.mp4", offset)

    stage_progress.record(db, job, stage_progress.SAMPLING_FRAMES)
    candidates = ffmpeg_ops.sample_distinct_frames(
        trimmed,
        work_dir / "frames",
        fps=settings.video_sample_fps,
        width=settings.video_sample_width,
        crop_w=settings.video_overlay_crop_width_fraction,
        crop_h=settings.video_overlay_crop_height_fraction,
        scene_threshold=settings.video_sample_scene_threshold,
    )
    logger.info("Sampled %d distinct visual states for job %s", len(candidates), job.id)
    _guard_candidate_count(job, len(candidates))

    stage_progress.record(db, job, stage_progress.UPLOADING_ARTIFACTS)
    frames_prefix = artifact_store.upload_artifacts(
        str(job.id), candidates, transcript.rebase(cues, offset)
    )

    video_ref = _publish_to_vimeo(db, job, trimmed, video_title(job))

    # Before the transcode wait rather than after it: the poster frame is what
    # the video shows while Vimeo is still working on it.
    if thumbnail.thumbnail_url(job):
        stage_progress.record(db, job, stage_progress.SETTING_THUMBNAIL)
        thumbnail.apply_thumbnail(job, video_ref)

    stage_progress.record(db, job, stage_progress.AWAITING_TRANSCODE)
    wait_for_transcode(video_ref)
    _delete_zoom_copy(db, job)

    job_service.advance(
        db,
        job,
        JobState.CHAPTERING,
        trim_offset_seconds=round(offset, 3),
        trim_fallback_used=fallback,
        frames_prefix=frames_prefix,
        source_duration_seconds=job.source_duration_seconds
        or int(ffmpeg_ops.probe_duration(source)),
    )
