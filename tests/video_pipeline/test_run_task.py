"""The ECS task entrypoint's claim check and fail-safe wrapper.

The task runs unattended on a machine nobody is watching. Every ending has to be
written to the job row: a crash that exits without recording anything leaves the
job stuck in `processing` until the sweeper times it out 90 minutes later, with
no reason an admin can read.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from src.video_pipeline import job_service, run_task
from src.video_pipeline.states import JobState

RECORDING_UUID = "zZ9/yY8+xX7=="


@pytest.fixture
def wired(monkeypatch, sessionmaker_factory):
    """Point the entrypoint at the in-memory DB and stub the failure alert."""
    monkeypatch.setattr(run_task, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr("src.video_pipeline.notify.notify_failure", lambda *a, **kw: None)
    return sessionmaker_factory


@pytest.fixture
def job(db, webinar):
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    return row


def test_unknown_job_id_exits_nonzero_without_touching_the_pipeline(wired, monkeypatch):
    monkeypatch.setattr(
        run_task.process_recording,
        "process",
        lambda *a, **kw: pytest.fail("nothing to process"),
    )

    assert run_task.run(str(uuid.uuid4())) == 1


def test_a_pending_job_is_claimed_before_processing(wired, db, job, monkeypatch):
    """Hand-launched runs skip the dispatcher, so the task claims the job itself."""
    seen: list[str] = []
    monkeypatch.setattr(
        run_task.process_recording, "process", lambda db_, j, work: seen.append(j.state)
    )

    assert run_task.run(str(job.id)) == 0
    assert seen == [JobState.PROCESSING.value]


def test_a_processing_job_runs_without_a_further_transition(wired, db, job, monkeypatch):
    """The dispatcher already claimed it — that is the normal path."""
    job_service.advance(db, job, JobState.PROCESSING)
    called: list[bool] = []
    monkeypatch.setattr(run_task.process_recording, "process", lambda *a: called.append(True))

    assert run_task.run(str(job.id)) == 0
    assert called == [True]


def test_an_already_published_job_is_a_no_op(wired, db, job, monkeypatch):
    """A replayed RunTask must not re-download, re-upload, or re-publish."""
    job_service.advance(db, job, JobState.PROCESSING)
    job_service.advance(db, job, JobState.CHAPTERING)
    job_service.advance(db, job, JobState.PUBLISHED)
    monkeypatch.setattr(
        run_task.process_recording, "process", lambda *a: pytest.fail("must not re-run")
    )

    # Exit 0, not 1: ECS treats a nonzero exit as a task failure, and there is
    # nothing wrong here.
    assert run_task.run(str(job.id)) == 0


def test_an_exception_is_recorded_on_the_job_rather_than_just_raised(wired, db, job, monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("ffmpeg exited 1")

    monkeypatch.setattr(run_task.process_recording, "process", explode)

    assert run_task.run(str(job.id)) == 1

    db.expire_all()
    refreshed = db.get(type(job), job.id)
    assert refreshed.job_state is JobState.FAILED
    assert "ffmpeg exited 1" in refreshed.error
    # The traceback goes with it — "ffmpeg exited 1" alone does not say where.
    assert "Traceback" in refreshed.error


def test_recorded_error_is_bounded(wired, db, job, monkeypatch):
    """The column is capped; an unbounded traceback would fail the write itself."""

    def explode(*a, **kw):
        raise RuntimeError("x" * 10_000)

    monkeypatch.setattr(run_task.process_recording, "process", explode)
    run_task.run(str(job.id))

    db.expire_all()
    assert len(db.get(type(job), job.id).error) <= 4000


def test_main_parses_the_job_id_argument(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(run_task, "run", lambda job_id: seen.append(job_id) or 0)

    assert run_task.main(["--job-id", "abc-123"]) == 0
    assert seen == ["abc-123"]


def test_the_entrypoint_can_resolve_its_relationships_on_its_own(tmp_path):
    """A fresh interpreter running only this module must map cleanly.

    `WebinarVideoJob.webinar` is a string relationship, resolved out of the
    mapper registry the first time the ORM is used. The API process fills that
    registry as a side effect of importing its routers, which hides a missing
    import here — but the task boots with nothing else, so a gap means the very
    first query dies with "failed to locate a name ('Webinar')" before the job
    is even read. Only a separate process proves it, since the test session has
    already imported every model.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.video_pipeline.run_task;"
            "from sqlalchemy.orm import configure_mappers;"
            "configure_mappers()",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
