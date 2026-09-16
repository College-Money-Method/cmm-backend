"""Job creation idempotency, transition validation, and the concurrency count."""

from __future__ import annotations

import uuid

import pytest

from src.video_pipeline import job_service
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import IllegalTransition, JobState, is_legal

RECORDING_UUID = "abc123XYZ=="


def test_same_recording_creates_exactly_one_job(db, webinar):
    """Zoom retries webhooks; three deliveries must leave one row."""
    results = [
        job_service.create_from_recording(
            db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
        )
        for _ in range(3)
    ]

    assert [created for _, created in results] == [True, False, False]
    assert len({job.id for job, _ in results}) == 1
    assert db.query(WebinarVideoJob).count() == 1


def test_duplicate_survives_a_stale_read(db, webinar, monkeypatch):
    """The UNIQUE constraint, not the pre-check, is what enforces idempotency.

    Simulates two concurrent deliveries racing: the second delivery's pre-check
    runs before the first has committed, so it sees nothing and attempts its own
    insert. The insert has to lose against the constraint and return the winning
    row instead of raising or creating a second job.
    """
    first, created = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    assert created

    original = job_service.get_by_recording_uuid
    calls = {"n": 0}

    def stale_on_first_call(*args, **kwargs):
        # Only the pre-check reads stale. The post-conflict re-select must see
        # the committed winner, exactly as it would against a real database.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original(*args, **kwargs)

    monkeypatch.setattr(job_service, "get_by_recording_uuid", stale_on_first_call)

    second, created_again = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )

    assert calls["n"] >= 2, "the conflict path must re-select rather than trust the pre-check"
    assert created_again is False
    assert second.id == first.id
    assert db.query(WebinarVideoJob).count() == 1


def test_new_job_starts_pending(db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    assert job.state == JobState.PENDING.value
    assert job.attempt == 0
    assert job.error is None


def test_happy_path_transitions_are_allowed(db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    job_service.advance(db, job, JobState.PROCESSING, ecs_task_arn="arn:task/1")
    assert job.ecs_task_arn == "arn:task/1"
    job_service.advance(db, job, JobState.CHAPTERING)
    job_service.advance(db, job, JobState.PUBLISHED, vimeo_video_id="987654321")
    assert job.state == JobState.PUBLISHED.value
    assert job.vimeo_video_id == "987654321"


@pytest.mark.parametrize(
    "source,target",
    [
        (JobState.PENDING, JobState.CHAPTERING),
        (JobState.PENDING, JobState.PUBLISHED),
        (JobState.PROCESSING, JobState.PUBLISHED),
        (JobState.PUBLISHED, JobState.PROCESSING),
        (JobState.PUBLISHED, JobState.FAILED),
        (JobState.FAILED, JobState.PROCESSING),
    ],
)
def test_illegal_transitions_are_rejected(db, webinar, source, target):
    """A skipped stage produces output that looks plausible and is wrong."""
    assert not is_legal(source, target)

    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=str(uuid.uuid4())
    )
    job.state = source.value
    db.commit()

    with pytest.raises(IllegalTransition):
        job_service.advance(db, job, target)

    db.refresh(job)
    assert job.state == source.value


def test_retry_rearms_a_failed_job(db, webinar, monkeypatch):
    monkeypatch.setattr("src.video_pipeline.notify.notify_failure", lambda *a, **k: False)

    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    job_service.advance(db, job, JobState.PROCESSING, ecs_task_arn="arn:task/1")
    job_service.fail(db, job, "ffmpeg exited 1")
    assert job.state == JobState.FAILED.value
    assert job.error == "ffmpeg exited 1"

    job_service.retry(db, job)

    assert job.state == JobState.PENDING.value
    assert job.attempt == 1
    assert job.error is None
    assert job.ecs_task_arn is None
    # Cleared so a second failure alerts again rather than being swallowed.
    assert job.failed_notified_at is None


def test_requeue_hands_a_slot_back_without_failing_the_job(db, webinar):
    """A task that found no source yet has done nothing worth keeping, so it
    goes back to `pending` for the sweeper rather than to `failed` for an admin."""
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    job_service.advance(db, job, JobState.PROCESSING, ecs_task_arn="arn:task/1")

    job_service.requeue(db, job, "Waiting for Zoom to finish processing the recording")

    assert job.state == JobState.PENDING.value
    assert job.attempt == 1
    assert job.ecs_task_arn is None
    # Kept, not cleared: half an hour of this should be visible on the screen.
    assert "Waiting for Zoom" in job.error


def test_in_flight_count_covers_only_processing(db, webinar):
    """`chaptering` runs inside the API and occupies no ECS task slot."""
    states = [
        JobState.PENDING,
        JobState.PROCESSING,
        JobState.PROCESSING,
        JobState.CHAPTERING,
        JobState.PUBLISHED,
        JobState.FAILED,
    ]
    for state in states:
        db.add(
            WebinarVideoJob(
                webinar_id=webinar.id,
                zoom_recording_uuid=str(uuid.uuid4()),
                state=state.value,
            )
        )
    db.commit()

    assert job_service.count_in_flight(db) == 2


def test_list_pending_is_oldest_first(db, webinar):
    for _ in range(3):
        job_service.create_from_recording(
            db, webinar_id=webinar.id, zoom_recording_uuid=str(uuid.uuid4())
        )

    pending = job_service.list_pending(db)
    assert len(pending) == 3
    assert pending == sorted(pending, key=lambda j: j.created_at)
