"""Dispatch decisions: the concurrency cap, and every reason to stay pending.

The cap exists because each task is a whole Fargate container doing a 90-minute
transcode. Over-dispatching does not merely queue — it saturates the account's
Fargate quota and starves unrelated tasks.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.video_pipeline import job_service, task_dispatch
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState


@pytest.fixture
def configured(monkeypatch):
    """Pretend the ECS values from cmm-infra are present."""
    monkeypatch.setattr(settings, "ecs_cluster_arn", "arn:aws:ecs:us-east-1:1:cluster/cmm")
    monkeypatch.setattr(settings, "video_task_definition_arn", "arn:aws:ecs:us-east-1:1:task-definition/video:1")
    monkeypatch.setattr(settings, "video_task_subnets", "subnet-aaa,subnet-bbb")
    monkeypatch.setattr(settings, "video_pipeline_max_concurrent", 3)


@pytest.fixture
def launched(monkeypatch):
    """Record RunTask calls instead of reaching AWS."""
    calls: list[str] = []

    def fake_run_task(job_id: str) -> str:
        calls.append(job_id)
        return f"arn:aws:ecs:us-east-1:1:task/{job_id}"

    monkeypatch.setattr(task_dispatch, "_run_task", fake_run_task)
    return calls


def _pending_job(db, webinar) -> WebinarVideoJob:
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    return job


def test_dispatch_moves_the_job_to_processing(db, webinar, configured, launched):
    job = _pending_job(db, webinar)

    assert task_dispatch.dispatch(db, job) is True
    assert job.job_state is JobState.PROCESSING
    assert job.ecs_task_arn is not None
    assert launched == [str(job.id)]


def test_cap_holds_extra_jobs_pending(db, webinar, configured, launched):
    """Three launch; the fourth waits for a slot rather than launching anyway."""
    jobs = [_pending_job(db, webinar) for _ in range(4)]

    results = [task_dispatch.dispatch(db, job) for job in jobs]

    assert results == [True, True, True, False]
    assert len(launched) == 3
    assert jobs[3].job_state is JobState.PENDING


def test_capacity_frees_when_a_job_finishes(db, webinar, configured, launched):
    """The cap counts what is running now, not what has ever run."""
    jobs = [_pending_job(db, webinar) for _ in range(4)]
    for job in jobs[:3]:
        task_dispatch.dispatch(db, job)
    assert task_dispatch.dispatch(db, jobs[3]) is False

    # First task hands off to chaptering, which runs in the API and holds no slot.
    job_service.advance(db, jobs[0], JobState.CHAPTERING)

    assert task_dispatch.dispatch(db, jobs[3]) is True


def test_unconfigured_leaves_the_job_pending(db, webinar, launched, monkeypatch):
    """Local dev has no cluster. The job is still created and still visible."""
    monkeypatch.setattr(settings, "ecs_cluster_arn", "")
    job = _pending_job(db, webinar)

    assert task_dispatch.dispatch(db, job) is False
    assert job.job_state is JobState.PENDING
    assert launched == []


def test_run_task_error_leaves_the_job_pending(db, webinar, configured, monkeypatch):
    """ECS throttling is transient — the sweeper retries rather than failing the job."""
    def boom(job_id: str) -> str:
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(task_dispatch, "_run_task", boom)
    job = _pending_job(db, webinar)

    assert task_dispatch.dispatch(db, job) is False
    assert job.job_state is JobState.PENDING


def test_a_job_already_running_is_not_dispatched_again(db, webinar, configured, launched):
    job = _pending_job(db, webinar)
    task_dispatch.dispatch(db, job)
    launched.clear()

    assert task_dispatch.dispatch(db, job) is False
    assert launched == []
