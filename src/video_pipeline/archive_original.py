"""Archive the untrimmed Zoom source to S3, and read it back on a retry.

The source is not worthless once Vimeo has transcoded it — it is the only thing
that makes a job re-runnable. Without an archive, Zoom's 7-day auto-delete puts
a hard 7-day expiry on the admin retry button: notice a bad trim in week two and
there is nothing left to re-run from.

Two rules follow from that:

* The archive is written **before** anything that can fail. A job that dies
  during sampling or upload is still retryable.
* The key is recorded on the job row rather than recomputed from a convention,
  so a later change to the prefix cannot orphan existing archives.

Storage is Glacier Instant Retrieval set on the PUT (not via a transition rule,
which would bill a transition request per object). Retention is a bucket
lifecycle rule in cmm-infra, never application code — a rule cannot be forgotten
in a code path that did not run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from src.config import settings
from src.storage.s3_client import s3_client

logger = logging.getLogger(__name__)

VIDEO_FILENAME = "source.mp4"
TRANSCRIPT_FILENAME = "source.vtt"
# Zoom's camera-only rendition, kept for trailer reels. Zoom's copy is deleted
# at publish, so without this a reel of a published webinar has no presenter shot.
CAMERA_FILENAME = "camera.mp4"


class ArchiveError(RuntimeError):
    """The original could not be archived or read back."""


def archive_prefix(job_id: str) -> str:
    """S3 prefix holding one job's untrimmed source."""
    return f"{settings.video_archive_prefix.strip('/')}/{job_id}/"


def expires_at(archived_at: datetime | None = None) -> datetime:
    """When the lifecycle rule will remove this archive.

    Stored on the job so the admin screen can disable retry with a reason rather
    than offering a button that cannot possibly work.
    """
    started = archived_at or datetime.now(timezone.utc)
    return started + timedelta(days=settings.video_archive_retention_days)


def _put(local: Path, key: str, content_type: str) -> None:
    """Upload one file. boto3's ``upload_file`` handles multipart on its own."""
    try:
        s3_client().upload_file(
            str(local),
            settings.s3_bucket_name,
            key,
            ExtraArgs={
                "StorageClass": settings.video_archive_storage_class,
                "ContentType": content_type,
            },
        )
    except (BotoCoreError, ClientError) as exc:
        raise ArchiveError(f"Archiving {local.name} to s3://{settings.s3_bucket_name}/{key} failed: {exc}") from exc
    logger.info("Archived %s → s3://%s/%s", local.name, settings.s3_bucket_name, key)


def archive_original(
    job_id: str, video: Path, transcript: Path | None, camera: Path | None = None
) -> str:
    """Copy the untrimmed mp4 and raw vtt to S3. Returns the prefix that holds them.

    Raises ArchiveError. Failing the job here is deliberate: continuing without
    an archive would publish a video that can never be re-processed, and the
    reason it could not be archived (bad bucket, missing permission) will apply
    to the frame uploads later in the same run anyway.
    """
    if not settings.s3_bucket_name:
        raise ArchiveError("S3_BUCKET_NAME is not configured — cannot archive the original")

    prefix = archive_prefix(job_id)
    _put(video, f"{prefix}{VIDEO_FILENAME}", "video/mp4")
    if transcript is not None:
        _put(transcript, f"{prefix}{TRANSCRIPT_FILENAME}", "text/vtt")
    if camera is not None:
        try:
            _put(camera, f"{prefix}{CAMERA_FILENAME}", "video/mp4")
        except ArchiveError as exc:
            # Only reels read it; the replay does not depend on it.
            logger.warning("Camera rendition not archived — %s", exc)
    return prefix


def archived_original_exists(prefix: str, filename: str = VIDEO_FILENAME) -> bool:
    """True when the archived file (default the mp4) is still in the bucket."""
    if not (prefix and settings.s3_bucket_name):
        return False
    try:
        s3_client().head_object(Bucket=settings.s3_bucket_name, Key=f"{prefix}{filename}")
        return True
    except (BotoCoreError, ClientError):
        return False


def download_archived(prefix: str, filename: str, dest: Path) -> Path:
    """Download one archived file to `dest`. Raises ArchiveError."""
    if not settings.s3_bucket_name:
        raise ArchiveError("S3_BUCKET_NAME is not configured — cannot read the archive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3_client().download_file(settings.s3_bucket_name, f"{prefix}{filename}", str(dest))
    except (BotoCoreError, ClientError) as exc:
        raise ArchiveError(f"Could not read {filename} from the archive at {prefix}: {exc}") from exc
    return dest


def read_archived_text(prefix: str, filename: str) -> str:
    """One small archived text file (the raw VTT), as a string. Raises ArchiveError."""
    if not settings.s3_bucket_name:
        raise ArchiveError("S3_BUCKET_NAME is not configured — cannot read the archive")
    try:
        body = s3_client().get_object(Bucket=settings.s3_bucket_name, Key=f"{prefix}{filename}")
        return body["Body"].read().decode("utf-8")
    except (BotoCoreError, ClientError) as exc:
        raise ArchiveError(f"Could not read {filename} from the archive at {prefix}: {exc}") from exc


def restore_original(prefix: str, work_dir: Path) -> tuple[Path, Path | None]:
    """Download an archived source back to ``work_dir`` for a re-run.

    Glacier Instant Retrieval is a plain GET — no restore job, no wait. Returns
    ``(video_path, transcript_path_or_None)``.
    """
    if not settings.s3_bucket_name:
        raise ArchiveError("S3_BUCKET_NAME is not configured — cannot restore the original")

    work_dir.mkdir(parents=True, exist_ok=True)
    client = s3_client()
    video_path = work_dir / VIDEO_FILENAME
    try:
        client.download_file(settings.s3_bucket_name, f"{prefix}{VIDEO_FILENAME}", str(video_path))
    except (BotoCoreError, ClientError) as exc:
        raise ArchiveError(f"Could not read the archived original at {prefix}: {exc}") from exc

    transcript_path: Path | None = work_dir / TRANSCRIPT_FILENAME
    try:
        client.download_file(
            settings.s3_bucket_name, f"{prefix}{TRANSCRIPT_FILENAME}", str(transcript_path)
        )
    except (BotoCoreError, ClientError):
        # Archived without a transcript, which is a state the original run
        # already handled — fall back to silence detection again.
        logger.warning("No archived transcript under %s — continuing without it", prefix)
        transcript_path = None

    logger.info("Restored archived original from s3://%s/%s", settings.s3_bucket_name, prefix)
    return video_path, transcript_path
