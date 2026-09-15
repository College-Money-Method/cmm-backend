"""What an audit run does differently once it is running.

Three differences, and each one is the difference between a safe audit and an
unsafe one: the video goes into the audit folder or nowhere at all, the source
recording is left alone (the last step of a publish, which an audit skips), and
the Vimeo video is labelled so it stays identifiable if it is ever moved out of
that folder.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.integrations.vimeo import VimeoError
from src.video_pipeline import job_service, process_recording, publish_service, stage_progress
from src.video_pipeline.video_title import video_title
from src.video_pipeline.states import JobState

FOLDER = "/users/151255816/projects/30467578"


@pytest.fixture
def audit_folder(monkeypatch):
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", FOLDER)


def _job(db, *, webinar_id=None, audit_only=False, source_url=None):
    job, _ = job_service.create_from_recording(
        db,
        zoom_recording_uuid=f"rec-{uuid.uuid4()}",
        webinar_id=webinar_id,
        source_url=source_url,
        audit_only=audit_only,
    )
    return job


# ── where the video lands ────────────────────────────────────────────────────


def test_an_audit_run_uploads_into_the_audit_folder(db, audit_folder):
    assert process_recording._upload_folder(_job(db, audit_only=True)) == FOLDER


def test_a_production_run_is_not_diverted_into_it(db, webinar, audit_folder):
    """The folder is for unreviewed videos. A real replay belongs in the library
    the site already embeds from."""
    assert process_recording._upload_folder(_job(db, webinar_id=webinar.id)) is None


def test_an_audit_run_refuses_to_upload_with_no_folder_configured(db, monkeypatch):
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "")

    with pytest.raises(VimeoError, match="VIMEO_AUDIT_FOLDER_URI"):
        process_recording._upload_folder(_job(db, audit_only=True))


# ── how the video is named ───────────────────────────────────────────────────


def test_an_audit_run_video_is_labelled_as_one(db, webinar):
    """The folder already separates it, but a video moved or shared out of the
    folder would otherwise look like a replay a school is watching."""
    title = video_title(_job(db, webinar_id=webinar.id, audit_only=True))

    assert title == "[Audit] Paying for College"


def test_an_audit_run_with_no_webinar_is_named_after_its_job(db):
    job = _job(db, audit_only=True)

    assert video_title(job) == f"[Audit] Webinar replay {job.id}"


def test_a_production_run_is_not_labelled(db, webinar):
    job = _job(db, webinar_id=webinar.id)

    assert video_title(job) == "Paying for College"


# ── the source is left alone ─────────────────────────────────────────────────


def test_an_audit_run_does_not_delete_the_zoom_recording(db, monkeypatch):
    """The recording may still be waiting for its real production run. Deleting
    it to prove the pipeline works on it would destroy what it proved."""
    monkeypatch.setattr(
        publish_service.zoom,
        "delete_recording",
        lambda uuid_: pytest.fail("must not delete a source it only audited"),
    )

    publish_service._delete_zoom_copy(db, _job(db, audit_only=True))


def test_a_url_source_has_no_zoom_copy_to_delete(db, monkeypatch):
    monkeypatch.setattr(
        publish_service.zoom,
        "delete_recording",
        lambda uuid_: pytest.fail("there is no Zoom recording behind a pasted URL"),
    )

    publish_service._delete_zoom_copy(
        db, _job(db, audit_only=True, source_url="https://example.com/a.mp4")
    )


def test_a_production_run_still_frees_the_zoom_pool(db, webinar, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        publish_service.zoom, "delete_recording", lambda uuid_: deleted.append(uuid_) or True
    )
    job = _job(db, webinar_id=webinar.id)

    publish_service._delete_zoom_copy(db, job)

    assert deleted == [job.zoom_recording_uuid]


# ── where the bytes come from ────────────────────────────────────────────────


def test_a_url_source_is_downloaded_from_its_url(db, monkeypatch, tmp_path):
    fetched: list[str] = []

    def fake_fetch(url, dest):
        fetched.append(url)
        dest.write_bytes(b"mp4")
        return dest

    monkeypatch.setattr(process_recording.url_recording_fetch, "fetch_from_url", fake_fetch)
    monkeypatch.setattr(
        process_recording,
        "fetch_recording",
        lambda *a: pytest.fail("a pasted URL must not be looked up in Zoom"),
    )
    job = _job(db, audit_only=True, source_url="https://cmm-media.s3.amazonaws.com/a.mp4")

    video, transcript, duration = process_recording._download_source(job, tmp_path)

    assert fetched == ["https://cmm-media.s3.amazonaws.com/a.mp4"]
    assert video.read_bytes() == b"mp4"
    # No VTT and no reported duration: the trim falls back to silence detection
    # and the duration is probed off the file, as it does for a Zoom account
    # with audio transcript switched off.
    assert transcript is None
    assert duration == 0


def test_a_zoom_source_still_goes_through_zoom(db, monkeypatch, tmp_path):
    from src.video_pipeline.zoom_recording_fetch import FetchedRecording

    video = tmp_path / "source.mp4"
    video.write_bytes(b"mp4")
    vtt = tmp_path / "source.vtt"
    vtt.write_text("WEBVTT\n")
    monkeypatch.setattr(
        process_recording,
        "fetch_recording",
        lambda uuid_, work_dir: FetchedRecording(
            video_path=video, transcript_path=vtt, duration_seconds=3600, topic="Session"
        ),
    )

    assert process_recording._download_source(_job(db, audit_only=True), tmp_path) == (
        video,
        vtt,
        3600,
    )


# ── the step timeline ────────────────────────────────────────────────────────


def test_freeing_the_zoom_pool_is_recorded_as_a_step(db, webinar, monkeypatch):
    monkeypatch.setattr(publish_service.zoom, "delete_recording", lambda uuid_: True)
    job = _job(db, webinar_id=webinar.id)

    publish_service._delete_zoom_copy(db, job)

    assert stage_progress.current(job) == stage_progress.DELETING_ZOOM_COPY


def test_a_step_the_run_never_takes_is_never_recorded(db, monkeypatch):
    """An audit run keeps the recording, so a delete step on its timeline would
    say it did something it is designed not to do."""
    monkeypatch.setattr(publish_service.zoom, "delete_recording", lambda uuid_: True)
    job = _job(db, audit_only=True)

    publish_service._delete_zoom_copy(db, job)

    assert stage_progress.current(job) == stage_progress.QUEUED
