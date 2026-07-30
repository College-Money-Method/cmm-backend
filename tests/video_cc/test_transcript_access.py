"""Transcript download.

Covers key resolution for archived transcripts, including the guardrail that
keys come from the registry rather than from the caller — corrected captions are
re-uploaded on Vimeo directly, so there is no publish path here.
"""

import pytest

import src.db.models  # noqa: F401 — instantiating an ORM model configures mappers,
# which needs the full model graph registered (relationships resolve by name).
from src.content import video_caption_archive as archive
from src.content.video_caption_models import VideoCaptionRecord

VTT = (
    "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHola.\n\n"
    "2\n00:00:02.000 --> 00:00:04.000\nBuenos días.\n"
)


@pytest.fixture
def record():
    return VideoCaptionRecord(
        video_id="123",
        privacy_hash="hash",
        title="A Video",
        source_s3_key="video-cc/123/source-aaa.vtt",
        translations={
            "es": {"language": "Spanish", "s3_key": "video-cc/123/es-bbb.vtt"},
            "zh": {"language": "Chinese"},  # older run, never archived
        },
    )


def test_key_resolution_covers_source_and_locales(record):
    assert archive.resolve_transcript_key(record, "source") == "video-cc/123/source-aaa.vtt"
    assert archive.resolve_transcript_key(record, "es") == "video-cc/123/es-bbb.vtt"


@pytest.mark.parametrize("label", ["zh", "fr", "../../etc/passwd", ""])
def test_unknown_or_unarchived_labels_resolve_to_nothing(record, label):
    """Keys come from the record, so a crafted label cannot reach other objects."""
    assert archive.resolve_transcript_key(record, label) is None


def test_presign_failure_returns_none_rather_than_raising(monkeypatch):
    class Boom:
        def generate_presigned_url(self, *a, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(archive, "get_s3_client", lambda: Boom())
    assert archive.presign_transcript("k", "f.vtt") is None
