"""Asset URL helpers bridging raw S3 object URLs and the public CDN.

Design (see plans/260730-1527-cloudfront-cdn-for-assets):
- The DB always stores the *raw S3 object URL*. Public URLs are rewritten to
  the CDN host at *read time*. This makes switching CDN domains (e.g.
  cdn.next.* -> cdn.*) a single config change (``settings.cdn_base_url``) with
  no data migration.
- ``to_cdn_url`` is idempotent and only rewrites S3 URLs, so it is safe to
  apply to any URL field: external links (Zoom, Vimeo, Airtable, Google Docs)
  and already-CDN URLs pass through untouched.
"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import PlainSerializer

from src.config import settings

# Substring that identifies an S3 virtual-hosted-style URL. Everything after it
# is the object key.
_S3_MARKER = ".amazonaws.com/"


def s3_object_url(s3_key: str) -> str:
    """Canonical raw S3 URL for an object key — the value persisted in the DB."""
    return (
        f"https://{settings.s3_bucket_name}.s3."
        f"{settings.aws_region}.amazonaws.com/{s3_key}"
    )


def to_cdn_url(value: Optional[str]) -> Optional[str]:
    """Rewrite an S3 object URL to the configured CDN host. Idempotent.

    Returns ``value`` unchanged when ``cdn_base_url`` is unset (kill-switch) or
    when ``value`` is not an S3 URL (external link / already a CDN URL).
    """
    if not value or not settings.cdn_base_url or _S3_MARKER not in value:
        return value
    key = value.split(_S3_MARKER, 1)[1].lstrip("/")
    return f"{settings.cdn_base_url.rstrip('/')}/{key}"


# Annotated ``str | None`` that rewrites S3 -> CDN when serialized. Use in
# response schemas for any field that may hold an uploaded asset URL.
CdnUrl = Annotated[Optional[str], PlainSerializer(to_cdn_url, return_type=Optional[str])]
