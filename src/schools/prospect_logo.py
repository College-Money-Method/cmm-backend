"""Validation and storage of a logo uploaded through the public fit check.

The endpoint is open to the internet, so the file type is decided by its leading
bytes rather than the client-declared type, and SVG is refused outright (a
public SVG is a script-injection vector).
"""

from __future__ import annotations

import io
import logging
import uuid

from fastapi import HTTPException, UploadFile
from PIL import Image

from src.config import settings
from src.schools.logo_thumbnail import generate_logo_thumbnail
from src.storage.asset_url import s3_object_url

logger = logging.getLogger(__name__)

MAX_LOGO_BYTES = 2 * 1024 * 1024
# A small compressed file can declare huge dimensions (decompression bomb);
# cap pixels before the thumbnail step decodes it.
MAX_LOGO_PIXELS = 4096 * 4096
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _logo_error(message: str) -> HTTPException:
    """422 with a structured detail so the form can show ``message`` as-is."""
    return HTTPException(status_code=422, detail={"code": "invalid_logo", "message": message})


def sniff_image_type(content: bytes) -> tuple[str, str] | None:
    """(extension, content type) for PNG/JPEG/WebP by magic bytes, else None."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


async def read_logo(upload: UploadFile | None) -> tuple[bytes, str, str] | None:
    """Read and validate an optional logo; returns (bytes, ext, content_type)."""
    if upload is None or not upload.filename:
        return None
    content = await upload.read(MAX_LOGO_BYTES + 1)
    if not content:
        return None
    if len(content) > MAX_LOGO_BYTES:
        raise _logo_error("Logo must be 2 MB or smaller.")
    kind = sniff_image_type(content)
    if kind is None:
        raise _logo_error("Logo must be a PNG, JPEG, or WebP image.")
    _check_dimensions(content)
    return content, kind[0], kind[1]


def _check_dimensions(content: bytes) -> None:
    """Reject files Pillow cannot parse, and oversized images, reading only the
    header (``Image.open`` is lazy, so nothing is decoded here)."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            width, height = img.size
    except Exception:
        raise _logo_error("Logo must be a PNG, JPEG, or WebP image.")
    if width * height > MAX_LOGO_PIXELS:
        raise _logo_error("Logo must be 4096 × 4096 pixels or smaller.")


def store_logo(s3, school_id: uuid.UUID, logo: tuple[bytes, str, str]) -> tuple[str, str] | None:
    """Upload logo + thumbnail; returns raw (logo_url, thumb_url) or None on failure.

    URLs are the canonical S3 form that the schools table stores; response
    schemas rewrite them to the CDN host. A storage failure must not lose the
    lead, so it is logged and the school is created without a logo.
    """
    content, ext, content_type = logo
    try:
        key = f"uploads/school-logos/{school_id}/{uuid.uuid4()}.{ext}"
        s3.put_object(Bucket=settings.s3_bucket_name, Key=key, Body=content,
                      ContentType=content_type, CacheControl=_IMMUTABLE_CACHE_CONTROL)
        url = s3_object_url(key)
        thumb_url = url
        thumb = generate_logo_thumbnail(content)
        if thumb:
            thumb_key = f"uploads/school-logos/{school_id}/{uuid.uuid4()}-thumb.webp"
            s3.put_object(Bucket=settings.s3_bucket_name, Key=thumb_key, Body=thumb,
                          ContentType="image/webp", CacheControl=_IMMUTABLE_CACHE_CONTROL)
            thumb_url = s3_object_url(thumb_key)
        return url, thumb_url
    except Exception:
        logger.exception("Prospect logo upload failed for school %s", school_id)
        return None
