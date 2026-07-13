#!/usr/bin/env python3
"""Upload one or more image URLs to S3 and print the permanent S3 URLs.

Usage:
    python scripts/upload_to_s3.py <url> [<url> ...]
    python scripts/upload_to_s3.py https://www.figma.com/api/mcp/asset/abc123

Each URL is downloaded, uploaded to the configured S3 bucket under the
`portal/assets/` prefix, and the resulting S3 URL is printed to stdout.

Configuration is read from the .env file in the backend root (same as the app).

Dependencies: boto3, requests, python-dotenv (all already in the project).
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import sys
from pathlib import Path

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

# Load settings from .env via the project's config
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import settings  # noqa: E402


S3_PREFIX = "portal/assets"
RESIZE_SIZES = [128, 256, 512]


def _ext_from_content_type(content_type: str) -> str:
    """Best-guess file extension from a Content-Type header value."""
    # Strip parameters like '; charset=utf-8'
    mime = content_type.split(";")[0].strip()
    ext = mimetypes.guess_extension(mime)
    # Python maps image/jpeg -> .jpeg; normalise common ones
    return {".jpeg": ".jpg", ".jpe": ".jpg", None: ".bin"}.get(ext, ext or ".bin")


def _put_object(s3_client, bucket: str, key: str, body: bytes, content_type: str) -> str:
    """Upload *body* to S3 and return the public HTTPS URL."""
    s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    region = settings.aws_region
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def _load_source(source: str) -> tuple[bytes, str]:
    """Load raw bytes and MIME type from a URL or local file path."""
    path = Path(source).expanduser()
    if path.exists():
        print(f"  Reading local file {path}...", file=sys.stderr)
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        return data, mime or "application/octet-stream"
    # Treat as URL
    print(f"  Downloading {source[:80]}...", file=sys.stderr)
    resp = requests.get(source, timeout=30)
    resp.raise_for_status()
    mime = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    return resp.content, mime


def upload_url(source: str, s3_client, bucket: str) -> dict[str, str]:
    """Upload a URL or local file path to S3 (original + resized variants).

    Returns a dict mapping ``"original"`` and each size (e.g. ``128``) to its
    public S3 HTTPS URL.
    """
    data, mime = _load_source(source)
    ext = _ext_from_content_type(mime)

    # Use a hash of the source (URL or resolved path) as the S3 key so
    # re-uploading the same asset is idempotent.
    source_key = str(Path(source).expanduser().resolve()) if Path(source).expanduser().exists() else source
    url_hash = hashlib.sha256(source_key.encode()).hexdigest()[:16]

    results: dict[str, str] = {}

    # Upload original
    original_key = f"{S3_PREFIX}/{url_hash}{ext}"
    print(f"  Uploading original to s3://{bucket}/{original_key}", file=sys.stderr)
    results["original"] = _put_object(s3_client, bucket, original_key, data, mime)

    # Resize and upload each size
    image = Image.open(io.BytesIO(data))
    for size in RESIZE_SIZES:
        resized = image.copy()
        resized.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = image.format or "PNG"
        resized.save(buf, format=fmt)
        buf.seek(0)

        sized_key = f"{S3_PREFIX}/{url_hash}.{size}{ext}"
        print(f"  Uploading {size}px to s3://{bucket}/{sized_key}", file=sys.stderr)
        results[str(size)] = _put_object(s3_client, bucket, sized_key, buf.read(), mime)

    return results


def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        print("Usage: python scripts/upload_to_s3.py <url-or-path> [<url-or-path> ...]", file=sys.stderr)
        sys.exit(1)

    if not settings.s3_bucket_name:
        print("Error: s3_bucket_name is not set in .env", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )

    for url in urls:
        try:
            results = upload_url(url, s3, settings.s3_bucket_name)
            for label, s3_url in results.items():
                print(f"{label}: {s3_url}")
        except (requests.RequestException, BotoCoreError, ClientError, OSError) as exc:
            print(f"Error uploading {url}: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
