"""The periodic pass that keeps the pipeline moving with no webhook to prompt it.

Its reason to exist is the deadlock: the cap counts `processing`, so tasks that
die without advancing state hold slots forever and the whole pipeline stops.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.video_pipeline import caption_task, job_service, notify, sweeper, task_dispatch
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ecs_cluster_arn", "arn:aws:ecs:us-east-1:1:cluster/cmm")
    monkeypatch.setattr(settings, "video_task_definition_arn", "arn:aws:ecs:us-east-1:1:task-definition/video:1")
    monkeypatch.setattr(settings, "video_task_subnets", "subnet-aaa")
    monkeypatch.setattr(settings, "video_pipeline_max_concurrent", 3)
    monkeypatch.setattr(task_dispatch, "_run_task", lambda job_id: f"arn:task/{job_id}")
    monkeypatch.setattr(notify, "send_email", lambda db, **kwargs: None)
    monkeypatch.setattr(settings, "video_pipeline_alert_email", "video-alerts@collegemoneymethod.com")


def _pending_job(db, webinar) -> WebinarVideoJob:
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    return job


def _age(db, job, minutes: int) -> None:
    """Backdate `updated_at`, which the server default otherwise pins to now."""
    job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.commit()


def test_a_dead_task_is_failed_and_its_slot_returned(db, webinar, configured):
    job = _pending_job(db, webinar)
    task_dispatch.dispatch(db, job)
    _age(db, job, minutes=120)

    assert sweeper.fail_stuck_jobs(db) == 1
    assert job.job_state is JobState.FAILED
    assert job_service.count_in_flight(db) == 0


def test_a_slow_but_live_task_is_left_alone(db, webinar, configured):
    """Failing a merely slow job costs a full re-download of the source."""
    job = _pending_job(db, webinar)
    task_dispatch.dispatch(db, job)
    _age(db, job, minutes=30)

    assert sweeper.fail_stuck_jobs(db) == 0
    assert job.job_state is JobState.PROCESSING


def test_the_backlog_drains_up_to_the_cap(db, webinar, configured):
    for _ in range(5):
        _pending_job(db, webinar)

    assert sweeper.dispatch_pending_jobs(db) == 3
    assert job_service.count_in_flight(db) == 3
    # The remaining two wait for the next pass rather than launching over the cap.
    assert sweeper.dispatch_pending_jobs(db) == 0


def test_one_pass_recovers_from_a_full_deadlock(db, webinar, configured):
    """Slots are freed before dispatch, so recovery takes one pass, not two."""
    stuck = []
    for _ in range(3):
        job = _pending_job(db, webinar)
        task_dispatch.dispatch(db, job)
        stuck.append(job)
    waiting = _pending_job(db, webinar)

    for job in stuck:
        _age(db, job, minutes=120)

    assert sweeper.fail_stuck_jobs(db) == 3
    assert sweeper.dispatch_pending_jobs(db) == 1
    assert waiting.job_state is JobState.PROCESSING


def _chaptering_job(db, webinar):
    job = _pending_job(db, webinar)
    job_service.advance(db, job, JobState.PROCESSING)
    job_service.advance(db, job, JobState.CHAPTERING)
    return job


def test_jobs_left_in_chaptering_are_finished_by_the_sweep(db, webinar, configured, monkeypatch):
    """The ECS task stops at `chaptering`; nothing else would ever pick these up."""
    job = _chaptering_job(db, webinar)
    monkeypatch.setattr(
        sweeper.publish_service,
        "publish",
        lambda db_, j: job_service.advance(db_, j, JobState.PUBLISHED),
    )

    assert sweeper.publish_chaptering_jobs(db) == 1
    assert job.job_state is JobState.PUBLISHED


def test_one_failed_job_neither_stops_the_batch_nor_stays_silent(db, webinar, configured, monkeypatch):
    first = _chaptering_job(db, webinar)
    second = _chaptering_job(db, webinar)

    def publish(db_, j):
        if j.id == first.id:
            raise RuntimeError("Bedrock refused every frame")
        job_service.advance(db_, j, JobState.PUBLISHED)

    monkeypatch.setattr(sweeper.publish_service, "publish", publish)

    assert sweeper.publish_chaptering_jobs(db) == 1
    assert first.job_state is JobState.FAILED
    assert "Bedrock refused every frame" in first.error
    assert second.job_state is JobState.PUBLISHED


def test_publishing_is_batched_so_one_sweep_cannot_run_for_an_hour(db, webinar, configured, monkeypatch):
    """Each job is a vision call per sampled frame — a whole backlog in one pass
    would hold the scheduler slot far past the sweep interval."""
    for _ in range(sweeper._PUBLISH_BATCH + 2):
        _chaptering_job(db, webinar)
    monkeypatch.setattr(
        sweeper.publish_service,
        "publish",
        lambda db_, j: job_service.advance(db_, j, JobState.PUBLISHED),
    )

    assert sweeper.publish_chaptering_jobs(db) == sweeper._PUBLISH_BATCH


def _published_job(db, webinar):
    job = _chaptering_job(db, webinar)
    job_service.advance(db, job, JobState.PUBLISHED)
    job.vimeo_video_id = "1225339276"
    db.commit()
    return job


def test_captions_are_taken_up_after_the_replay_is_already_live(db, webinar, configured, monkeypatch):
    job = _published_job(db, webinar)
    monkeypatch.setattr(sweeper.caption_task, "run", lambda db_, j: caption_task.COMPLETED)

    assert sweeper.caption_published_jobs(db) == 1


def test_a_caption_run_that_blows_up_leaves_the_replay_published(db, webinar, configured, monkeypatch):
    """Translated subtitles are a nicety; a live replay is not. Nothing in this
    step is allowed to disturb what schools already see."""
    job = _published_job(db, webinar)

    def boom(db_, j):
        raise RuntimeError("Bedrock throttled")

    monkeypatch.setattr(sweeper.caption_task, "run", boom)

    assert sweeper.caption_published_jobs(db) == 0
    assert job.job_state is JobState.PUBLISHED
