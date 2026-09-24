"""Trailer reels: what can be cut from, the admin endpoints, and the reel task.

Rendering itself (Bedrock, ffmpeg, Transcribe) is covered by ``test_trailer``
and the local debug script; here it is replaced by a stub so the rules around
it — one render at a time, blocked jobs, abandoned tasks, Vimeo upload — are
tested against real rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.integrations import vimeo, vimeo_upload, zoom
from src.video_pipeline import (
    archive_original,
    frame_urls,
    job_service,
    reel_service,
    reel_sources,
    reel_task,
    task_dispatch,
)
from src.video_pipeline.reel_build import BuiltReel
from src.video_pipeline.reel_models import FAILED, PENDING, READY, RENDERING, WebinarVideoReel
from src.video_pipeline.reel_router import router
from src.video_pipeline.reel_sources import ReelInputs
from src.video_pipeline.states import JobState
from src.video_pipeline.trailer_select import build_prompt
from src.video_pipeline.transcript import Cue
from src.video_pipeline.zoom_recording_fetch import select_camera_file

BASE = "/api/v1/admin/video-pipeline"


@pytest.fixture
def published_job(db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job_service.advance(db, job, JobState.PROCESSING)
    job_service.advance(db, job, JobState.CHAPTERING, frames_prefix="video-pipeline/frames/p/")
    job_service.advance(db, job, JobState.PUBLISHED, chapters=[])
    job.trim_offset_seconds = Decimal("12.5")
    job.archive_key = f"video-pipeline/originals/{job.id}/"
    db.commit()
    return job


@pytest.fixture
def archive(monkeypatch):
    """Which archived files exist: the original always, the camera when asked."""
    present = {archive_original.VIDEO_FILENAME}
    monkeypatch.setattr(archive_original, "archived_original_exists",
                        lambda prefix, filename=archive_original.VIDEO_FILENAME:
                        filename in present)
    return present


@pytest.fixture
def zoom_recording(monkeypatch):
    """What Zoom answers for the recording; None-able, or an exception to raise."""
    answer: dict = {"payload": {"recording_files": []}}

    def get_recording(_uuid):
        if isinstance(answer["payload"], Exception):
            raise answer["payload"]
        return answer["payload"]

    monkeypatch.setattr(zoom, "get_recording", get_recording)
    return answer


@pytest.fixture
def launched(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: True)
    monkeypatch.setattr(task_dispatch, "_run_reel_task",
                        lambda reel_id: calls.append(reel_id) or f"arn:task/{reel_id}")
    return calls


@pytest.fixture
def client(sessionmaker_factory, monkeypatch):
    monkeypatch.setattr(frame_urls, "presign", lambda key, expires_in=3600: f"https://s3/{key}")
    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        db = sessionmaker_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), email="admin@collegemoneymethod.com", role="super_admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _reel(db, job, state=READY, **fields) -> WebinarVideoReel:
    reel = WebinarVideoReel(job_id=job.id, orientation="portrait", state=state, **fields)
    db.add(reel)
    db.commit()
    return reel


# --- sources -------------------------------------------------------------------------------

def test_the_camera_file_is_the_completed_active_speaker_mp4():
    payload = {"recording_files": [
        {"recording_type": "speaker_view", "file_type": "MP4", "download_url": "u1"},
        {"recording_type": "active_speaker", "file_type": "MP4", "download_url": "u2"},
        {"recording_type": "shared_screen_with_speaker_view", "file_type": "MP4",
         "download_url": "u3"},
    ]}
    assert select_camera_file(payload)["download_url"] == "u2"


def test_an_unfinished_camera_file_does_not_count():
    payload = {"recording_files": [
        {"recording_type": "active_speaker", "file_type": "MP4", "download_url": "u",
         "status": "processing"},
    ]}
    assert select_camera_file(payload) is None


def test_the_admin_direction_leads_the_prompt_and_blank_adds_nothing():
    cues = [Cue(start=0, end=5, text="Hello.")]
    steered = build_prompt(cues, [], "T", [True], focus="  Focus on merit aid ")
    assert steered.startswith("Editorial direction for this reel: Focus on merit aid\n")
    assert build_prompt(cues, [], "T", [True], focus="  ").startswith("Webinar title: T")


def test_an_unpublished_job_is_blocked(db, webinar, archive):
    job, _ = job_service.create_from_recording(db, webinar_id=webinar.id,
                                               zoom_recording_uuid="rec-new")
    assert "published" in reel_sources.blocked_reason(job)


def test_an_expired_archive_is_blocked(published_job, archive):
    published_job.archive_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert "expired" in reel_sources.blocked_reason(published_job)


def test_an_archived_camera_is_enough(published_job, archive, zoom_recording):
    archive.add(archive_original.CAMERA_FILENAME)
    zoom_recording["payload"] = AssertionError("Zoom must not be asked")
    assert reel_sources.blocked_reason(published_job) is None


def test_an_older_job_can_use_zoom_while_zoom_has_the_camera(published_job, archive,
                                                             zoom_recording):
    zoom_recording["payload"] = {"recording_files": [
        {"recording_type": "active_speaker", "file_type": "MP4", "download_url": "u"}]}
    assert reel_sources.blocked_reason(published_job) is None


def test_an_older_job_whose_zoom_copy_is_gone_is_blocked(published_job, archive,
                                                         zoom_recording):
    zoom_recording["payload"] = zoom.ZoomApiError("3301 recording does not exist")
    assert "Zoom no longer has" in reel_sources.blocked_reason(published_job)


# --- endpoints -----------------------------------------------------------------------------

def test_creating_a_reel_launches_its_task(client, published_job, archive, launched):
    archive.add(archive_original.CAMERA_FILENAME)
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"orientation": "portrait", "prompt": " Focus on merit aid "})

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == RENDERING and body["stage"] == "starting"
    assert body["prompt"] == "Focus on merit aid"
    assert launched == [body["id"]]


def test_without_ecs_the_reel_waits_pending(client, published_job, archive, monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"orientation": "landscape"})
    assert response.status_code == 201
    assert response.json()["state"] == PENDING


def test_one_reel_renders_at_a_time(client, db, published_job, archive, launched):
    archive.add(archive_original.CAMERA_FILENAME)
    _reel(db, published_job, state=RENDERING)
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"orientation": "landscape"})
    assert response.status_code == 409
    assert launched == []


def test_the_database_holds_one_active_reel_per_job(db, published_job):
    _reel(db, published_job, state=RENDERING)
    _reel(db, published_job, state=FAILED)
    with pytest.raises(IntegrityError):
        _reel(db, published_job, state=PENDING)
    db.rollback()


def test_a_request_that_loses_the_race_is_a_conflict(client, db, published_job, archive,
                                                      launched, monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    _reel(db, published_job, state=RENDERING)
    # As if the other request committed between this one's check and insert.
    monkeypatch.setattr(reel_service, "list_reels", lambda _db, _job: [])
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"orientation": "landscape"})
    assert response.status_code == 409
    assert launched == []


def test_a_launch_ecs_refuses_fails_the_reel(client, published_job, archive, monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: True)

    def refuse(_reel_id):
        raise RuntimeError("capacity unavailable")

    monkeypatch.setattr(task_dispatch, "_run_reel_task", refuse)
    body = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                       json={"orientation": "landscape"}).json()
    assert body["state"] == FAILED and "capacity unavailable" in body["error"]


def test_a_blocked_job_refuses_with_the_reason(client, published_job, archive,
                                               zoom_recording, launched):
    zoom_recording["payload"] = zoom.ZoomApiError("gone")
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"orientation": "landscape"})
    assert response.status_code == 409
    assert "Zoom no longer has" in response.json()["detail"]


def test_an_unknown_orientation_or_long_prompt_is_rejected(client, published_job):
    url = f"{BASE}/jobs/{published_job.id}/reels"
    assert client.post(url, json={"orientation": "square"}).status_code == 422
    assert client.post(url, json={"orientation": "portrait",
                                  "prompt": "x" * 501}).status_code == 422


def test_the_list_previews_ready_reels_and_fails_abandoned_ones(client, db, published_job,
                                                                archive):
    archive.add(archive_original.CAMERA_FILENAME)
    ready = _reel(db, published_job, s3_key="video-pipeline/reels/j/r.mp4",
                  duration_seconds=Decimal("58.4"), hook_title="Hook")
    stuck = _reel(db, published_job, state=RENDERING)
    stuck.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()

    body = client.get(f"{BASE}/jobs/{published_job.id}/reels").json()

    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(ready.id)]["preview_url"] == "https://s3/video-pipeline/reels/j/r.mp4"
    assert by_id[str(ready.id)]["duration_seconds"] == 58.4
    assert by_id[str(stuck.id)]["state"] == FAILED
    assert by_id[str(stuck.id)]["preview_url"] is None
    assert body["blocked_reason"] is None


def test_uploading_a_ready_reel_records_the_vimeo_video(client, db, published_job,
                                                        monkeypatch):
    reel = _reel(db, published_job, s3_key="video-pipeline/reels/j/r.mp4", hook_title="Hook")
    uploaded: list[str] = []

    class FakeS3:
        def download_file(self, _bucket, _key, dest):
            Path(dest).write_bytes(b"mp4")

    monkeypatch.setattr(reel_service, "s3_client", lambda: FakeS3())
    monkeypatch.setattr(vimeo_upload, "create_video",
                        lambda path, name, **kw: uploaded.append(name)
                        or {"video_id": "123", "hash": "abc"})

    response = client.post(f"{BASE}/reels/{reel.id}/vimeo")

    assert response.status_code == 200
    assert response.json()["vimeo_url"] == "https://vimeo.com/123/abc"
    assert uploaded == ["Paying for College — Sept — trailer (portrait)"]
    assert client.post(f"{BASE}/reels/{reel.id}/vimeo").status_code == 409


def test_an_unfinished_reel_cannot_be_uploaded(client, db, published_job):
    reel = _reel(db, published_job, state=RENDERING)
    assert client.post(f"{BASE}/reels/{reel.id}/vimeo").status_code == 409


def test_a_vimeo_refusal_is_a_bad_gateway(client, db, published_job, monkeypatch):
    reel = _reel(db, published_job, s3_key="k")

    class FakeS3:
        def download_file(self, _bucket, _key, dest):
            Path(dest).write_bytes(b"mp4")

    def refuse(*_a, **_kw):
        raise vimeo.VimeoError("quota exceeded")

    monkeypatch.setattr(reel_service, "s3_client", lambda: FakeS3())
    monkeypatch.setattr(vimeo_upload, "create_video", refuse)
    response = client.post(f"{BASE}/reels/{reel.id}/vimeo")
    assert response.status_code == 502
    assert "quota" in response.json()["detail"]


# --- the task ------------------------------------------------------------------------------

@pytest.fixture
def task_env(sessionmaker_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(reel_task, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr(reel_task, "require_ffmpeg", lambda: None)
    monkeypatch.setattr(reel_sources, "load_inputs", lambda job: ReelInputs(
        title="T", cues=[Cue(start=0, end=1, text="Hi.")], chapters=[], trim_offset=12.5))
    monkeypatch.setattr(reel_sources, "download_camera_video", lambda job, dest: dest)
    monkeypatch.setattr(reel_sources, "download_screen", lambda job, dest: dest)
    uploads: list[str] = []

    class FakeS3:
        def upload_file(self, _path, _bucket, key, ExtraArgs=None):
            uploads.append(key)

    monkeypatch.setattr(reel_task, "s3_client", lambda: FakeS3())
    return uploads


def test_the_task_renders_uploads_and_marks_the_reel_ready(db, published_job, task_env,
                                                           monkeypatch):
    reel = _reel(db, published_job, state=RENDERING, prompt="Focus on merit aid")
    seen: dict = {}

    def build(inputs, **kw):
        seen.update(kw)
        kw["on_stage"]("rendering")
        return BuiltReel(path=kw["work_dir"] / "reel.mp4", hook_title="Hook",
                         duration_seconds=59.2)

    monkeypatch.setattr(reel_task, "build_reel", build)

    assert reel_task.run(str(reel.id)) == 0

    db.expire_all()
    done = db.get(WebinarVideoReel, reel.id)
    assert done.state == READY and done.stage is None
    assert done.s3_key == f"video-pipeline/reels/{published_job.id}/{reel.id}.mp4"
    assert task_env == [done.s3_key]
    assert float(done.duration_seconds) == 59.2 and done.hook_title == "Hook"
    assert seen["focus"] == "Focus on merit aid" and seen["orientation"] == "portrait"
    assert seen["scratch_prefix"] == f"video-pipeline/reels/{published_job.id}/tmp"


def test_a_failing_task_records_why(db, published_job, task_env, monkeypatch):
    reel = _reel(db, published_job, state=RENDERING)

    def boom(*_a, **_kw):
        raise RuntimeError("Bedrock said no")

    monkeypatch.setattr(reel_task, "build_reel", boom)

    assert reel_task.run(str(reel.id)) == 1
    db.expire_all()
    failed = db.get(WebinarVideoReel, reel.id)
    assert failed.state == FAILED and "Bedrock said no" in failed.error


def test_a_reel_failed_while_rendering_stays_failed(db, published_job, task_env,
                                                    sessionmaker_factory, monkeypatch):
    reel = _reel(db, published_job, state=RENDERING)

    def slow_build(inputs, **kw):
        # The list endpoint gives up on the reel while this task is still going.
        other = sessionmaker_factory()
        other.get(WebinarVideoReel, reel.id).state = FAILED
        other.commit()
        other.close()
        return BuiltReel(path=kw["work_dir"] / "reel.mp4", hook_title="Hook",
                         duration_seconds=59.2)

    monkeypatch.setattr(reel_task, "build_reel", slow_build)

    assert reel_task.run(str(reel.id)) == 0
    db.expire_all()
    late = db.get(WebinarVideoReel, reel.id)
    assert late.state == FAILED and late.s3_key is None


def test_a_finished_reel_is_not_rendered_again(db, published_job, task_env, monkeypatch):
    reel = _reel(db, published_job, state=READY)
    monkeypatch.setattr(reel_task, "build_reel", lambda *a, **kw: pytest.fail("rendered"))
    assert reel_task.run(str(reel.id)) == 0


def test_a_camera_that_will_not_archive_does_not_fail_the_job(monkeypatch, tmp_path):
    monkeypatch.setattr(archive_original.settings, "s3_bucket_name", "bucket")
    put: list[str] = []

    def fake_put(local, key, _content_type):
        if local.name == archive_original.CAMERA_FILENAME:
            raise archive_original.ArchiveError("AccessDenied")
        put.append(key.rsplit("/", 1)[-1])

    monkeypatch.setattr(archive_original, "_put", fake_put)
    prefix = archive_original.archive_original(
        "job-1", tmp_path / "source.mp4", tmp_path / "source.vtt",
        camera=tmp_path / archive_original.CAMERA_FILENAME)

    assert prefix
    assert put == [archive_original.VIDEO_FILENAME, archive_original.TRANSCRIPT_FILENAME]
