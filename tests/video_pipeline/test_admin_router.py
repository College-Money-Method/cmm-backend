"""Admin monitoring endpoints and the retry lever.

The pipeline is unattended, so this screen is where a failure becomes visible
and where the one manual recovery action lives.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.config import settings
from src.db.deps import get_db
from src.video_pipeline import frame_urls, job_service, manual_run, notify, task_dispatch
from src.video_pipeline.router import router
from src.video_pipeline.states import JobState

BASE = "/api/v1/admin/video-pipeline"


@pytest.fixture
def client(sessionmaker_factory, monkeypatch):
    monkeypatch.setattr(notify, "send_email", lambda db, **kwargs: None)
    monkeypatch.setattr(settings, "video_pipeline_alert_email", "video-alerts@collegemoneymethod.com")

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


@pytest.fixture
def failed_job(db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-failed"
    )
    job_service.advance(db, job, JobState.PROCESSING)
    job_service.fail(db, job, "ffmpeg exited 1")
    return job


def test_jobs_list_returns_the_webinar_context(client, failed_job, webinar):
    response = client.get(f"{BASE}/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["state"] == "failed"
    assert body["items"][0]["webinar_name"] == webinar.webinar_name


def test_jobs_list_filters_by_state(client, failed_job):
    assert client.get(f"{BASE}/jobs", params={"state": "failed"}).json()["total"] == 1
    assert client.get(f"{BASE}/jobs", params={"state": "published"}).json()["total"] == 0


def test_an_unknown_state_filter_is_rejected(client, failed_job):
    assert client.get(f"{BASE}/jobs", params={"state": "sideways"}).status_code == 422


def test_job_detail_exposes_the_error(client, failed_job):
    response = client.get(f"{BASE}/jobs/{failed_job.id}")

    assert response.status_code == 200
    assert response.json()["error"] == "ffmpeg exited 1"


def test_missing_job_is_a_404(client):
    assert client.get(f"{BASE}/jobs/{uuid.uuid4()}").status_code == 404


def test_retry_rearms_a_failed_job(client, failed_job, db, monkeypatch):
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)

    response = client.post(f"{BASE}/jobs/{failed_job.id}/retry")

    assert response.status_code == 200
    db.refresh(failed_job)
    assert failed_job.job_state is JobState.PENDING
    assert failed_job.attempt == 1
    assert failed_job.error is None


def test_retrying_a_running_job_is_rejected(client, db, webinar):
    """Retrying mid-flight would double-process a recording already in a task."""
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-running"
    )
    job_service.advance(db, job, JobState.PROCESSING)

    assert client.post(f"{BASE}/jobs/{job.id}/retry").status_code == 409


# ── what the list has to make obvious ────────────────────────────────────────


@pytest.fixture
def published_job(db, webinar):
    """A job that succeeded, but with two things worth a human glance."""
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-published"
    )
    job_service.advance(db, job, JobState.PROCESSING, trim_fallback_used=True)
    job_service.advance(db, job, JobState.CHAPTERING, frames_prefix="video-pipeline/frames/p/")
    job_service.advance(
        db,
        job,
        JobState.PUBLISHED,
        chapters_truncated=True,
        chapters=[
            {"timecode": 0, "title": "Introduction", "source": "intro", "confidence": ""},
            {"timecode": 60, "title": "The Aid Formula", "source": "title_card", "confidence": "match"},
            {"timecode": 900, "title": "Award Letters", "source": "title_card", "confidence": "no_match"},
        ],
    )
    return job


def test_a_quietly_imperfect_job_is_visible_without_opening_it(client, published_job):
    """It published like any other — nothing alerted on any of these."""
    row = client.get(f"{BASE}/jobs").json()["items"][0]

    assert row["state"] == "published"
    assert row["chapter_count"] == 3
    assert row["trim_fallback_used"] is True
    assert row["chapters_truncated"] is True
    assert row["unconfirmed_chapters"] == 1


def test_the_list_names_the_workshop_the_session_belongs_to(client, published_job):
    assert client.get(f"{BASE}/jobs").json()["items"][0]["workshop_name"] == "Paying for College"


def test_jobs_filter_by_workshop(client, published_job, db, webinar):
    from src.workshops.models import Workshop

    other = Workshop(id=uuid.uuid4(), name="Choosing a Major")
    db.add(other)
    db.commit()

    matching = client.get(f"{BASE}/jobs", params={"workshop_id": str(webinar.workshop_id)})
    assert matching.json()["total"] == 1
    assert client.get(f"{BASE}/jobs", params={"workshop_id": str(other.id)}).json()["total"] == 0


def test_pagination_reports_the_full_total_not_the_page_size(client, published_job, failed_job):
    body = client.get(f"{BASE}/jobs", params={"limit": 1, "offset": 1}).json()

    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["offset"] == 1


# ── frames ───────────────────────────────────────────────────────────────────


def test_frames_come_back_presigned_with_their_expiry(client, published_job, monkeypatch):
    monkeypatch.setattr(
        frame_urls,
        "for_job",
        lambda job, *a, **kw: [
            frame_urls.FrameRef(index=1, timestamp=12.5, filename="frame_0001.jpg", url="https://s3/x")
        ],
    )

    body = client.get(f"{BASE}/jobs/{published_job.id}/frames").json()

    assert body["expires_in"] == frame_urls.EXPIRES_IN
    assert body["items"] == [
        {"index": 1, "timestamp": 12.5, "filename": "frame_0001.jpg", "url": "https://s3/x"}
    ]


def test_frames_for_a_job_whose_images_have_expired_are_empty_not_an_error(
    client, published_job, monkeypatch
):
    monkeypatch.setattr(frame_urls, "for_job", lambda job, *a, **kw: [])

    response = client.get(f"{BASE}/jobs/{published_job.id}/frames")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_frames_for_a_missing_job_are_a_404(client):
    assert client.get(f"{BASE}/jobs/{uuid.uuid4()}/frames").status_code == 404


# ── retry, once the source is gone ───────────────────────────────────────────


def test_a_failed_job_reports_itself_retryable(client, failed_job):
    body = client.get(f"{BASE}/jobs/{failed_job.id}").json()

    assert body["retryable"] is True
    assert body["retry_blocked_reason"] is None


def test_retry_is_refused_once_the_archived_source_has_expired(client, failed_job, db):
    """Past the 90-day archive there is nothing left to re-process, so re-arming
    the job would only produce a second identical failure."""
    failed_job.archive_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    detail = client.get(f"{BASE}/jobs/{failed_job.id}").json()
    assert detail["retryable"] is False
    assert "expired" in detail["retry_blocked_reason"]
    assert detail["archive_expired"] is True

    assert client.post(f"{BASE}/jobs/{failed_job.id}/retry").status_code == 409
    db.refresh(failed_job)
    assert failed_job.job_state is JobState.FAILED


def test_retry_still_works_while_the_archive_is_alive(client, failed_job, db, monkeypatch):
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)
    failed_job.archive_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.commit()

    assert client.post(f"{BASE}/jobs/{failed_job.id}/retry").status_code == 200
    db.refresh(failed_job)
    assert failed_job.job_state is JobState.PENDING


def test_an_archive_still_in_date_is_not_reported_expired(client, failed_job, db):
    """The screen greys out the retry button from this flag, so a live archive
    reading as expired would take away the one action that still works."""
    failed_job.archive_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.commit()

    assert client.get(f"{BASE}/jobs/{failed_job.id}").json()["archive_expired"] is False


def test_a_job_that_was_never_archived_is_not_reported_expired(client, failed_job):
    """`archive_expires_at` is null for a job that failed before the archive
    step; that is unknown, not past."""
    assert failed_job.archive_expires_at is None
    assert client.get(f"{BASE}/jobs/{failed_job.id}").json()["archive_expired"] is False


def test_a_published_job_says_why_it_cannot_be_retried(client, published_job):
    body = client.get(f"{BASE}/jobs/{published_job.id}").json()

    assert body["retryable"] is False
    assert "failed" in body["retry_blocked_reason"]


# ── starting a run by hand ───────────────────────────────────────────────────


@pytest.fixture
def run_ready(monkeypatch):
    """An audit folder configured, Zoom answering, and ECS unavailable."""
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "/users/151255816/projects/30467578")
    monkeypatch.setattr(
        manual_run.zoom, "get_recording", lambda ref: {"uuid": "rec-pasted", "topic": "Session"}
    )
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)


def test_a_pasted_meeting_id_starts_an_audit_run(client, run_ready):
    response = client.post(f"{BASE}/runs", json={"source": "881 2345 6789"})

    assert response.status_code == 201
    body = response.json()
    assert body["job"]["audit_only"] is True
    assert body["job"]["webinar_id"] is None
    assert body["job"]["state"] == "pending"
    assert body["dispatched"] is False


def test_a_pasted_download_url_starts_an_audit_run(client, run_ready, monkeypatch):
    monkeypatch.setattr(
        manual_run.url_recording_fetch, "assert_public_url", lambda url: None
    )
    url = "https://cmm-media.s3.amazonaws.com/raw/session.mp4"

    body = client.post(f"{BASE}/runs", json={"source": url}).json()

    assert body["job"]["source_url"] == url
    assert body["job"]["audit_only"] is True


def test_attaching_a_webinar_only_names_the_run(client, run_ready, webinar, db):
    body = client.post(
        f"{BASE}/runs", json={"source": "88812345678", "webinar_id": str(webinar.id)}
    ).json()

    assert body["job"]["webinar_name"] == webinar.webinar_name
    db.refresh(webinar)
    assert webinar.video_embed_code is None


def test_an_unparseable_paste_comes_back_with_what_to_paste(client, run_ready):
    response = client.post(f"{BASE}/runs", json={"source": "the one from Tuesday"})

    assert response.status_code == 422
    assert "recording UUID" in response.json()["detail"]


def test_a_share_link_is_refused_by_the_endpoint(client, run_ready):
    response = client.post(
        f"{BASE}/runs", json={"source": "https://us02web.zoom.us/rec/share/AbCdEfGhIjKl"}
    )

    assert response.status_code == 422


def test_a_recording_that_already_has_a_job_is_a_409(client, run_ready, db, webinar):
    job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-pasted"
    )

    response = client.post(f"{BASE}/runs", json={"source": "88812345678"})

    assert response.status_code == 409
    assert "already has job" in response.json()["detail"]


def test_a_run_with_no_audit_folder_configured_is_refused(client, run_ready, monkeypatch):
    """Fail closed: without a folder the upload would land in the library the
    production replays live in, with nothing marking it unreviewed."""
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "")

    response = client.post(f"{BASE}/runs", json={"source": "88812345678"})

    assert response.status_code == 422
    assert "VIMEO_AUDIT_FOLDER_URI" in response.json()["detail"]


def test_an_unknown_webinar_is_refused_before_the_run_starts(client, run_ready):
    response = client.post(
        f"{BASE}/runs", json={"source": "88812345678", "webinar_id": str(uuid.uuid4())}
    )

    assert response.status_code == 422


def test_an_empty_source_is_rejected_by_the_schema(client, run_ready):
    assert client.post(f"{BASE}/runs", json={"source": ""}).status_code == 422


def test_an_audit_run_appears_in_the_job_list_marked_as_one(client, run_ready):
    client.post(f"{BASE}/runs", json={"source": "88812345678"})

    row = client.get(f"{BASE}/jobs").json()["items"][0]
    assert row["audit_only"] is True
    assert row["webinar_id"] is None
