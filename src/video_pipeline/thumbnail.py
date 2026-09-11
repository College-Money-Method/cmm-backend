"""Give a replay the poster frame its workshop carries, when it has one.

The image is read from the workshop at upload time, never re-applied. That is
what makes replacing a workshop's thumbnail affect only the sessions recorded
afterwards: the videos already published hold the frame that was current when
they were made, which is what an archive of a year's workshops should look like.

Nothing here is fatal. A poster frame is cosmetic, and by the time it is set the
video is already uploaded and about to be chaptered — failing the run over an
unreachable image would throw away an hour of processing for a thumbnail.
"""

from __future__ import annotations

import logging

import httpx

from src.integrations.vimeo_upload import set_thumbnail
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.url_recording_fetch import UrlFetchError, assert_public_url

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# A poster frame is a still. Anything this large is a mistake — a video pasted
# into the field, or a page that answered the request with HTML.
_MAX_BYTES = 10 * 1024 * 1024


def thumbnail_url(job: WebinarVideoJob) -> str | None:
    """The workshop's recording thumbnail, or None when there is nothing to set."""
    webinar = job.webinar
    workshop = getattr(webinar, "workshop", None) if webinar else None
    return (getattr(workshop, "recording_thumbnail_url", None) or "").strip() or None


def _download(url: str) -> bytes:
    """Fetch the image, refusing a private host or an oversized body."""
    assert_public_url(url)
    with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith("image/"):
            raise UrlFetchError(f"'{url}' answered with {content_type}, not an image")

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_BYTES:
                raise UrlFetchError(f"'{url}' is over the {_MAX_BYTES // 1024 // 1024}MB thumbnail limit")
            chunks.append(chunk)
    return b"".join(chunks)


def apply_thumbnail(job: WebinarVideoJob, video_ref: str) -> bool:
    """Set the workshop's thumbnail on ``video_ref``. Never raises.

    Returns True when a thumbnail was actually uploaded — False both for a
    workshop that has none and for an attempt that failed, because neither
    changes what the caller does next.
    """
    url = thumbnail_url(job)
    if not url:
        return False

    try:
        set_thumbnail(video_ref, _download(url))
        return True
    except Exception as exc:  # noqa: BLE001 — cosmetic step, never fatal
        logger.warning(
            "Could not set the thumbnail on %s from %s — leaving Vimeo's own frame (%s)",
            video_ref,
            url,
            exc,
        )
        return False


__all__ = ["apply_thumbnail", "thumbnail_url"]
