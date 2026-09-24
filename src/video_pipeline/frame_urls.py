"""URLs for one job's candidate frames, for the admin screen.

The frames are what the vision model saw, so putting them next to the chapter
titles turns "chapter 4 looks wrong" into something an admin can confirm in a
glance rather than by opening the video and scrubbing.

Where a CDN is configured (``settings.cdn_base_url``) the browser loads frames
and reel previews from it; the URL is stable and does not expire. Elsewhere
each object is handed out as a presigned GET that expires, and the URLs are
never logged.

The S3 lifecycle rule removes frames after 30 days while the job row keeps its
chapters forever. That is the normal end state for an old job, not an error, so
a prefix whose manifest has gone returns an empty list and the screen shows the
chapters without pictures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import settings
from src.storage.asset_url import s3_object_url, to_cdn_url
from src.storage.s3_client import get_s3_client
from src.video_pipeline import artifact_store
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

# Presigned only: long enough to read a chapter list, short enough that a URL
# pasted into a ticket is dead by the time anyone else opens it.
EXPIRES_IN = 900


@dataclass(frozen=True)
class FrameRef:
    """One sampled frame, addressable by the browser until ``EXPIRES_IN`` passes."""

    index: int
    timestamp: float
    filename: str
    url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "filename": self.filename,
            "url": self.url,
        }


def presign(key: str, expires_in: int = EXPIRES_IN) -> str | None:
    """A presigned GET for one object, or None if the URL could not be signed.

    Signing does not check that the key exists, so a frame the lifecycle rule
    has already removed still yields a URL; the browser gets the 403/404 and
    falls back to a placeholder. That is deliberate — a HEAD per frame would
    cost one request per chapter to learn something the image load reveals
    anyway.
    """
    if not settings.s3_bucket_name:
        return None
    try:
        return get_s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket_name, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:  # noqa: BLE001 — a signing failure must not 500 the screen
        logger.warning("video pipeline: could not presign a frame: %s", exc)
        return None


def object_url(key: str, expires_in: int = EXPIRES_IN) -> str | None:
    """The URL the browser loads ``key`` from: the CDN when set, else presigned S3."""
    if settings.cdn_base_url:
        return to_cdn_url(s3_object_url(key))
    return presign(key, expires_in)


def url_lifetime(expires_in: int = EXPIRES_IN) -> int | None:
    """Seconds an ``object_url`` stays valid; None when it does not expire."""
    return None if settings.cdn_base_url else expires_in


def for_job(job: WebinarVideoJob, expires_in: int = EXPIRES_IN) -> list[FrameRef]:
    """Every frame of ``job``, oldest first, each with a URL from ``object_url``.

    Reads ``candidates.json`` rather than listing the prefix so the timestamps
    come from the same manifest the chapters were built from — a frame shown
    beside a chapter is then the frame that produced it, not one that happens
    to sort nearby.
    """
    prefix = job.frames_prefix
    if not prefix:
        return []

    try:
        manifest = artifact_store.load_json_artifact(prefix, artifact_store.CANDIDATES_FILENAME)
    except artifact_store.ArtifactError as exc:
        # Expected once the 30-day lifecycle rule has run.
        logger.info("video pipeline: no frame manifest for job %s (%s)", job.id, exc)
        return []

    if not isinstance(manifest, list):
        logger.warning("video pipeline: frame manifest for job %s is not a list", job.id)
        return []

    frames: list[FrameRef] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        # The manifest opens with a synthetic entry carrying no file; it exists
        # to anchor the first chapter and there is no image behind it.
        filename = entry.get("file")
        if not filename:
            continue
        url = object_url(f"{prefix}{filename}", expires_in)
        if url is None:
            continue
        frames.append(
            FrameRef(
                index=int(entry.get("index", 0)),
                timestamp=float(entry.get("timestamp", 0.0)),
                filename=str(filename),
                url=url,
            )
        )

    frames.sort(key=lambda frame: frame.timestamp)
    return frames
