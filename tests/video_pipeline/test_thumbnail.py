"""The poster frame a workshop puts on its replays.

Two properties carry the feature. The image is read at upload time and never
re-applied, which is what makes changing a workshop's thumbnail affect only the
sessions recorded afterwards. And nothing here is fatal: by the time the
thumbnail is set the video is uploaded and about to be chaptered, so failing the
run over an unreachable image would throw away an hour of processing for a
picture.
"""

from __future__ import annotations

import uuid

import pytest

from src.video_pipeline import job_service, thumbnail
from src.workshops.models import Webinar, Workshop

IMAGE = "https://cdn.example.com/poster.png"


def _job(db, webinar=None):
    job, _ = job_service.create_from_recording(
        db,
        zoom_recording_uuid=f"rec-{uuid.uuid4()}",
        webinar_id=webinar.id if webinar else None,
    )
    return job


@pytest.fixture
def with_image(db, webinar):
    webinar.workshop.recording_thumbnail_url = IMAGE
    db.commit()
    return webinar


# ── where the image comes from ───────────────────────────────────────────────


def test_the_image_comes_from_the_jobs_workshop(db, with_image):
    assert thumbnail.thumbnail_url(_job(db, with_image)) == IMAGE


def test_a_workshop_with_no_image_asks_for_no_upload(db, webinar):
    assert thumbnail.thumbnail_url(_job(db, webinar)) is None


def test_a_blank_field_counts_as_no_image(db, webinar):
    webinar.workshop.recording_thumbnail_url = "   "
    db.commit()

    assert thumbnail.thumbnail_url(_job(db, webinar)) is None


def test_a_run_with_no_webinar_has_no_workshop_to_read(db):
    assert thumbnail.thumbnail_url(_job(db)) is None


# ── applying it ──────────────────────────────────────────────────────────────


def test_the_downloaded_bytes_are_what_reaches_vimeo(db, with_image, monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(thumbnail, "_download", lambda url: b"\x89PNG...")
    monkeypatch.setattr(
        thumbnail,
        "set_thumbnail",
        lambda ref, image: sent.update(ref=ref, image=image),
    )

    assert thumbnail.apply_thumbnail(_job(db, with_image), "42:hash") is True
    assert sent == {"ref": "42:hash", "image": b"\x89PNG..."}


def test_nothing_is_uploaded_for_a_workshop_without_an_image(db, webinar, monkeypatch):
    def fail(*args, **kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("Vimeo must not be called with nothing to send")

    monkeypatch.setattr(thumbnail, "set_thumbnail", fail)

    assert thumbnail.apply_thumbnail(_job(db, webinar), "42") is False


def test_an_unreachable_image_does_not_fail_the_published_video(db, with_image, monkeypatch):
    """The video is already on Vimeo by now. It keeps the frame Vimeo chose."""
    def boom(url):
        raise thumbnail.UrlFetchError("404")

    monkeypatch.setattr(thumbnail, "_download", boom)

    assert thumbnail.apply_thumbnail(_job(db, with_image), "42") is False


def test_a_vimeo_rejection_does_not_fail_the_published_video(db, with_image, monkeypatch):
    monkeypatch.setattr(thumbnail, "_download", lambda url: b"bytes")

    def boom(ref, image):
        raise RuntimeError("Vimeo said no")

    monkeypatch.setattr(thumbnail, "set_thumbnail", boom)

    assert thumbnail.apply_thumbnail(_job(db, with_image), "42") is False
