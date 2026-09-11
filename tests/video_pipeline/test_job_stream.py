"""The live progress stream: what goes on the wire, and when.

The generator is tested directly rather than through a client. A real stream
holds the connection for fifteen minutes and emits nothing until something
moves, which is exactly the behaviour under test — driving it in place is the
only way to assert the quiet case at all.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.video_pipeline import job_service, job_stream, stage_progress
from src.video_pipeline.router import router as jobs_router
from src.video_pipeline.states import JobState

BASE = "/api/v1/admin/video-pipeline"


def _drain(gen, limit: int = 20) -> list[str]:
    """Collect frames until the generator stops or ``limit`` is reached."""

    async def collect():
        out: list[str] = []
        async for frame in gen:
            out.append(frame)
            if len(out) >= limit:
                break
        return out

    return asyncio.run(collect())


@pytest.fixture
def fast_stream(monkeypatch):
    """Real timings compressed so a whole stream lifetime fits in a test."""
    monkeypatch.setattr(job_stream, "MAX_STREAM_SECONDS", 0.3)
    monkeypatch.setattr(job_stream, "HEARTBEAT_SECONDS", 0.05)


def _tick(payloads):
    """A loader that walks a script, then repeats its last entry forever."""
    seq = list(payloads)

    def load():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return load


def _data(frames: list[str]) -> list[dict]:
    return [json.loads(f[len("data: ") : -2]) for f in frames if f.startswith("data:")]


# ── frame emission ───────────────────────────────────────────────────────────


def test_the_first_frame_is_a_full_snapshot(fast_stream):
    """A reload reopens the stream, so a client that had to wait for the next
    change would render an empty stepper until something moved."""
    frames = _drain(job_stream._frames(_tick([{"type": "jobs", "jobs": []}]), lambda _p: 0.01))

    assert frames[0].startswith("data: ")
    assert _data(frames)[0] == {"type": "jobs", "jobs": []}


def test_an_unchanged_payload_is_not_re_sent(fast_stream):
    frames = _drain(
        job_stream._frames(_tick([{"type": "job", "job": {"state": "processing"}}]), lambda _p: 0.01)
    )

    assert len(_data(frames)) == 1


def test_a_change_emits_a_new_snapshot(fast_stream):
    load = _tick(
        [
            {"type": "job", "job": {"state": "processing", "stage": "trimming"}},
            {"type": "job", "job": {"state": "processing", "stage": "sampling_frames"}},
        ]
    )

    frames = _drain(job_stream._frames(load, lambda _p: 0.01))

    stages = [d["job"]["stage"] for d in _data(frames)]
    assert stages[:2] == ["trimming", "sampling_frames"]


def test_a_quiet_stream_sends_heartbeats(fast_stream):
    """The ALB sets no idle timeout, so it drops a stream that says nothing for
    a minute. Comment frames keep it open without inventing data."""
    frames = _drain(job_stream._frames(_tick([{"type": "jobs", "jobs": []}]), lambda _p: 0.01))

    assert ": keep-alive\n\n" in frames


def test_a_heartbeat_is_not_sent_while_changes_are_flowing(fast_stream):
    counter = {"n": 0}

    def load():
        counter["n"] += 1
        return {"type": "job", "job": {"stage": f"stage-{counter['n']}"}}

    frames = _drain(job_stream._frames(load, lambda _p: 0.01), limit=6)

    assert all(f.startswith("data:") for f in frames)


def test_a_deleted_job_ends_the_stream(fast_stream):
    """Nothing more will ever change, and the client needs to stop reconnecting."""
    frames = _drain(job_stream._frames(_tick([{"type": "missing"}]), lambda _p: 0.01))

    assert _data(frames) == [{"type": "missing"}]
    assert len(frames) == 1


def test_the_stream_closes_at_its_deadline(fast_stream):
    """Connections are recycled rather than held forever; the client reopens."""
    frames = _drain(job_stream._frames(_tick([{"type": "jobs", "jobs": []}]), lambda _p: 0.05), limit=500)

    assert len(frames) < 500


# ── poll cadence ─────────────────────────────────────────────────────────────


def test_an_in_flight_job_is_polled_at_the_active_interval():
    payload = {"type": "job", "job": {"state": JobState.PROCESSING.value}}

    assert job_stream._detail_interval(payload) == job_stream.ACTIVE_POLL_SECONDS


def test_a_settled_job_is_polled_slowly():
    """A published job only changes if someone retries it from this very page."""
    payload = {"type": "job", "job": {"state": JobState.PUBLISHED.value}}

    assert job_stream._detail_interval(payload) == job_stream.SETTLED_POLL_SECONDS


def test_a_failed_job_is_polled_slowly():
    payload = {"type": "job", "job": {"state": JobState.FAILED.value}}

    assert job_stream._detail_interval(payload) == job_stream.SETTLED_POLL_SECONDS


def test_a_missing_job_does_not_break_the_cadence_lookup():
    assert job_stream._detail_interval({"type": "missing"}) == job_stream.SETTLED_POLL_SECONDS


# ── what the loaders read ────────────────────────────────────────────────────


@pytest.fixture
def own_session(sessionmaker_factory, monkeypatch):
    """Point the stream's own session factory at the test database.

    The loaders deliberately do not use the request's session — they run on a
    worker thread and outlive the request — so this is the seam.
    """
    monkeypatch.setattr(job_stream, "get_session_factory", lambda: sessionmaker_factory)


def test_a_detail_frame_carries_the_timeline_and_the_plan(db, webinar, own_session):
    # A poster image so the plan is the whole of PLAN — a workshop without one
    # legitimately expects no thumbnail step.
    webinar.workshop.recording_thumbnail_url = "https://cdn.example.com/poster.png"
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job_service.advance(db, job, JobState.PROCESSING)
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)

    payload = job_stream._load_detail(job.id)

    assert payload["type"] == "job"
    assert [e["stage"] for e in payload["job"]["stage_events"]] == [
        stage_progress.QUEUED,
        stage_progress.FETCHING_SOURCE,
    ]
    assert payload["job"]["stage_plan"] == list(stage_progress.PLAN)
    assert payload["job"]["stage"] == stage_progress.FETCHING_SOURCE


def test_a_detail_frame_for_an_unknown_job_is_missing(own_session):
    assert job_stream._load_detail(uuid.uuid4()) == {"type": "missing"}


def test_the_active_frame_holds_only_jobs_still_in_flight(db, webinar, own_session):
    running, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-running"
    )
    job_service.advance(db, running, JobState.PROCESSING)
    settled, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid="rec-settled"
    )
    job_service.advance(db, settled, JobState.PROCESSING)
    job_service.advance(db, settled, JobState.CHAPTERING)
    job_service.advance(db, settled, JobState.PUBLISHED)

    payload = job_stream._load_active()

    assert [j["zoom_recording_uuid"] for j in payload["jobs"]] == ["rec-running"]
    assert payload["type"] == "jobs"


def test_an_active_summary_carries_the_current_stage(db, webinar, own_session):
    """The list page shows the stage inline, so it must not need the detail call."""
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job_service.advance(db, job, JobState.PROCESSING)
    stage_progress.record(db, job, stage_progress.UPLOADING_TO_VIMEO)

    payload = job_stream._load_active()

    assert payload["jobs"][0]["stage"] == stage_progress.UPLOADING_TO_VIMEO


def test_the_active_frame_is_newest_first(db, webinar, own_session):
    for i in range(3):
        job, _ = job_service.create_from_recording(
            db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{i}"
        )
        job.created_at = job.created_at.replace(year=2020 + i)
        job_service.advance(db, job, JobState.PROCESSING)

    payload = job_stream._load_active()

    assert [j["zoom_recording_uuid"] for j in payload["jobs"]] == ["rec-2", "rec-1", "rec-0"]


# ── routing ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client(sessionmaker_factory, monkeypatch, fast_stream):
    """Both routers mounted together, which is the ordering the app uses."""
    monkeypatch.setattr(job_stream, "get_session_factory", lambda: sessionmaker_factory)

    app = FastAPI()
    app.include_router(jobs_router)
    app.include_router(job_stream.router)

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


def test_the_active_stream_is_not_swallowed_by_the_job_detail_route(client):
    """`/jobs/active/stream` has three segments, so `/jobs/{job_id}` cannot
    match it and reject "active" as a malformed UUID."""
    res = client.get(f"{BASE}/jobs/active/stream")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert '"type": "jobs"' in res.text


def test_a_job_stream_serves_that_job(client, db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )

    res = client.get(f"{BASE}/jobs/{job.id}/stream")

    assert res.status_code == 200
    assert str(job.id) in res.text


def test_a_stream_is_marked_unbuffered(client):
    """Any proxy that buffers the body defeats the point of streaming it."""
    res = client.get(f"{BASE}/jobs/active/stream")

    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
