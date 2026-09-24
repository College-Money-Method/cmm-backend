"""Presigned frame URLs for the admin screen.

Two things matter here and neither is visible from a green screen: the frame
shown beside a chapter has to be the frame that produced it, and a job old
enough to have lost its frames has to degrade to "no pictures" rather than to
an error page over chapter data that is still perfectly good.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.video_pipeline import artifact_store, frame_urls, job_service
from src.video_pipeline.states import JobState

PREFIX = "video-pipeline/frames/job/"

MANIFEST = [
    {"index": 0, "timestamp": 0.0, "file": None},
    {"index": 2, "timestamp": 300.0, "file": "frame_0002.jpg"},
    {"index": 1, "timestamp": 12.5, "file": "frame_0001.jpg"},
]


class _S3:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.signed: list[tuple[str, int]] = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803 — boto3's spelling
        if self.fail:
            raise RuntimeError("no credentials")
        self.signed.append((Params["Key"], ExpiresIn))
        return f"https://s3.example/{Params['Key']}?sig=x&exp={ExpiresIn}"


@pytest.fixture
def job(db, webinar):
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job_service.advance(db, row, JobState.PROCESSING)
    job_service.advance(db, row, JobState.CHAPTERING, frames_prefix=PREFIX)
    return row


@pytest.fixture
def s3(monkeypatch):
    client = _S3()
    monkeypatch.setattr(settings, "s3_bucket_name", "cmm-media")
    monkeypatch.setattr(frame_urls, "get_s3_client", lambda: client)
    return client


def _manifest(monkeypatch, manifest):
    monkeypatch.setattr(
        frame_urls.artifact_store, "load_json_artifact", lambda prefix, filename: manifest
    )


def test_every_frame_comes_back_in_timeline_order(job, s3, monkeypatch):
    _manifest(monkeypatch, MANIFEST)

    frames = frame_urls.for_job(job)

    assert [f.timestamp for f in frames] == [12.5, 300.0]
    assert [f.filename for f in frames] == ["frame_0001.jpg", "frame_0002.jpg"]


def test_the_anchor_entry_is_skipped_because_no_image_exists_for_it(job, s3, monkeypatch):
    """`candidates.json` opens with a synthetic 0.0 entry carrying no file."""
    _manifest(monkeypatch, MANIFEST)

    frame_urls.for_job(job)

    assert [key for key, _ in s3.signed] == [
        f"{PREFIX}frame_0002.jpg",
        f"{PREFIX}frame_0001.jpg",
    ]


def test_urls_expire_within_the_quarter_hour(job, s3, monkeypatch):
    """A frame is a still of a school's session; a long-lived URL is a leak."""
    _manifest(monkeypatch, MANIFEST)

    frames = frame_urls.for_job(job)

    assert frame_urls.EXPIRES_IN == 900
    assert {expiry for _, expiry in s3.signed} == {900}
    assert all("exp=900" in f.url for f in frames)


def test_with_a_cdn_frames_load_from_it_and_nothing_is_signed(job, s3, monkeypatch):
    monkeypatch.setattr(settings, "cdn_base_url", "https://cdn.example.com")
    _manifest(monkeypatch, MANIFEST)

    frames = frame_urls.for_job(job)

    assert [f.url for f in frames] == [
        f"https://cdn.example.com/{PREFIX}frame_0001.jpg",
        f"https://cdn.example.com/{PREFIX}frame_0002.jpg",
    ]
    assert s3.signed == []
    assert frame_urls.url_lifetime() is None


def test_a_job_that_never_reached_the_frame_stage_has_none(db, webinar, s3, monkeypatch):
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-pending"
    )
    monkeypatch.setattr(
        frame_urls.artifact_store,
        "load_json_artifact",
        lambda *a: pytest.fail("S3 must not be read for a job with no prefix"),
    )

    assert frame_urls.for_job(row) == []


def test_expired_frames_are_not_an_error(job, s3, monkeypatch):
    """The 30-day lifecycle rule removing frames is the normal end state."""

    def gone(prefix, filename):
        raise artifact_store.ArtifactError("NoSuchKey")

    monkeypatch.setattr(frame_urls.artifact_store, "load_json_artifact", gone)

    assert frame_urls.for_job(job) == []


def test_a_frame_that_cannot_be_signed_is_dropped_rather_than_returned_broken(
    job, monkeypatch
):
    monkeypatch.setattr(settings, "s3_bucket_name", "cmm-media")
    monkeypatch.setattr(frame_urls, "get_s3_client", lambda: _S3(fail=True))
    _manifest(monkeypatch, MANIFEST)

    assert frame_urls.for_job(job) == []


def test_a_manifest_of_the_wrong_shape_does_not_crash_the_screen(job, s3, monkeypatch):
    _manifest(monkeypatch, {"unexpected": "object"})

    assert frame_urls.for_job(job) == []
