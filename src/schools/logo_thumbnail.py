"""Thumbnail generation for school logos.

Mirrors the approach used in scripts/migrate_logos_to_s3.py: resize to a
small square webp on a white background so list views load fast.
"""

import io

from PIL import Image

THUMB_SIZE = 200
THUMB_QUALITY = 85


def generate_logo_thumbnail(content: bytes) -> bytes | None:
    """Return webp thumbnail bytes for an uploaded logo image.

    Returns None when the image cannot be processed (e.g. SVG input,
    which Pillow cannot rasterize) — callers should fall back to the
    full-size logo URL in that case.
    """
    try:
        img = Image.open(io.BytesIO(content)).convert("RGBA")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        buf = io.BytesIO()
        background.save(buf, format="WEBP", quality=THUMB_QUALITY)
        return buf.getvalue()
    except Exception:
        return None
