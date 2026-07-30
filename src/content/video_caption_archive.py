"""S3 archival + Postgres registry for Video CC runs.

Every run archives the English source and each translated track to S3, and
upserts one ``video_caption_records`` row per video so the admin list can show
what has been processed without calling Vimeo.

Archiving is best-effort by design: a failed S3 write must not fail a job whose
captions are already live on Vimeo. The S3 key is simply left null.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.content.video_caption_models import VideoCaptionRecord
from src.storage import get_s3_client

logger = logging.getLogger(__name__)

# Transcripts live under one prefix, partitioned by video id.
_S3_PREFIX = "video-cc"


def split_video_ref(video_ref: str) -> tuple[str, str | None]:
    """Split "123:hash" into ("123", "hash"); ("123", None) when unlisted-free."""
    video_id, _, privacy_hash = video_ref.partition(":")
    return video_id, privacy_hash or None


def archive_transcript(video_id: str, label: str, content: str) -> str | None:
    """Upload a VTT to S3 and return its key, or None if the upload failed.

    ``label`` distinguishes the variant — "source" or a locale code. A short uuid
    suffix keeps re-runs from overwriting the previous archive, so the history of
    what was actually published stays intact.
    """
    key = f"{_S3_PREFIX}/{video_id}/{label}-{uuid.uuid4().hex[:8]}.vtt"
    try:
        get_s3_client().put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/vtt; charset=utf-8",
        )
        return key
    except Exception as exc:  # noqa: BLE001 — archival must never fail a job
        logger.warning("video_cc: could not archive %s to S3: %s", key, exc)
        return None


def _parse_vimeo_time(value: str | None) -> datetime | None:
    """Parse Vimeo's ISO-8601 created_time; None when absent or malformed."""
    if not value:
        return None
    try:
        # Vimeo emits "+00:00"; older payloads may use "Z".
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("video_cc: unparseable Vimeo created_time %r", value)
        return None


def upsert_record(
    db: Session,
    *,
    video_ref: str,
    metadata: dict,
    source_origin: str,
    source_name: str,
    source_cue_count: int,
    source_s3_key: str | None,
    translations: dict,
    status: str,
) -> VideoCaptionRecord:
    """Create or update the registry row for a video (no commit).

    New language results are merged into any existing ``translations`` map rather
    than replacing it, so translating Spanish today and Mandarin next week leaves
    both recorded.
    """
    video_id, privacy_hash = split_video_ref(video_ref)

    record = db.execute(
        select(VideoCaptionRecord).where(VideoCaptionRecord.video_id == video_id)
    ).scalar_one_or_none()

    if record is None:
        record = VideoCaptionRecord(id=uuid.uuid4(), video_id=video_id, translations={})
        db.add(record)

    record.privacy_hash = privacy_hash or record.privacy_hash
    record.title = metadata.get("name") or record.title
    record.vimeo_created_at = _parse_vimeo_time(metadata.get("created_time")) or record.vimeo_created_at
    record.duration_seconds = metadata.get("duration") or record.duration_seconds
    record.source_origin = source_origin
    record.source_name = source_name
    record.source_cue_count = source_cue_count
    if source_s3_key:
        record.source_s3_key = source_s3_key

    # Reassign rather than mutate: SQLAlchemy does not track in-place JSONB edits.
    record.translations = {**(record.translations or {}), **translations}
    record.last_status = status
    record.last_run_at = datetime.now(timezone.utc)
    return record


def presign_transcript(key: str, filename: str, expires_in: int = 300) -> str | None:
    """Short-lived download URL for an archived transcript, or None on failure.

    Presigned rather than a direct bucket URL so this keeps working if the bucket
    is ever made private (it is currently public). ``ResponseContentDisposition``
    makes the browser save it under a readable name instead of the uuid key.
    """
    try:
        return get_s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.s3_bucket_name,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": "text/vtt; charset=utf-8",
            },
            ExpiresIn=expires_in,
        )
    except Exception as exc:  # noqa: BLE001 — a missing object must not 500
        logger.warning("video_cc: could not presign %s: %s", key, exc)
        return None


def get_record(db: Session, video_id: str) -> VideoCaptionRecord | None:
    """Registry row for a video id (without privacy hash), or None."""
    return db.execute(
        select(VideoCaptionRecord).where(VideoCaptionRecord.video_id == video_id)
    ).scalar_one_or_none()


def resolve_transcript_key(record: VideoCaptionRecord, label: str) -> str | None:
    """S3 key for ``label`` — "source" or a locale code — on this record.

    Keys are read from the record rather than taken from the caller, so the
    download endpoint cannot be pointed at arbitrary bucket objects.
    """
    if label == "source":
        return record.source_s3_key
    entry = (record.translations or {}).get(label)
    return entry.get("s3_key") if isinstance(entry, dict) else None


def list_records(db: Session, limit: int = 200) -> list[VideoCaptionRecord]:
    """Processed videos, most recently run first."""
    return list(
        db.execute(
            select(VideoCaptionRecord)
            .order_by(VideoCaptionRecord.last_run_at.desc().nullslast())
            .limit(limit)
        ).scalars()
    )
