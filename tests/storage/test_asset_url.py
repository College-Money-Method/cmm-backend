"""Unit tests for S3 -> CDN asset URL rewriting.

The invariant that matters: only S3 object URLs get rewritten, and only when a
CDN host is configured. External links and already-rewritten CDN URLs must pass
through untouched, so the serializer is safe to attach to any URL field and
safe to run more than once.
"""

import pytest

from src.config import settings
from src.storage.asset_url import s3_object_url, to_cdn_url

S3_URL = "https://cmm-general.s3.us-east-1.amazonaws.com/assets/content/x/img.jpg"
CDN_URL = "https://cdn.next.collegemoneymethod.com/assets/content/x/img.jpg"


@pytest.fixture
def cdn_enabled(monkeypatch):
    monkeypatch.setattr(settings, "cdn_base_url", "https://cdn.next.collegemoneymethod.com")


def test_s3_object_url_builds_regional_url(monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket_name", "cmm-general")
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    assert s3_object_url("a/b.png") == "https://cmm-general.s3.us-east-1.amazonaws.com/a/b.png"


def test_rewrites_s3_url_to_cdn(cdn_enabled):
    assert to_cdn_url(S3_URL) == CDN_URL


def test_external_url_passes_through(cdn_enabled):
    for ext in ("https://zoom.us/j/123", "https://docs.google.com/d/1", ""):
        assert to_cdn_url(ext) == ext


def test_none_passes_through(cdn_enabled):
    assert to_cdn_url(None) is None


def test_idempotent_on_cdn_url(cdn_enabled):
    # Running twice (or on an already-CDN value) must not double-prefix.
    assert to_cdn_url(CDN_URL) == CDN_URL
    assert to_cdn_url(to_cdn_url(S3_URL)) == CDN_URL


def test_kill_switch_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "cdn_base_url", "")
    assert to_cdn_url(S3_URL) == S3_URL


def test_trailing_slash_in_config_is_normalised(monkeypatch):
    monkeypatch.setattr(settings, "cdn_base_url", "https://cdn.next.collegemoneymethod.com/")
    assert to_cdn_url(S3_URL) == CDN_URL
