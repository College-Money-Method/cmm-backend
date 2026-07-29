"""In-memory job registry for Video CC translation jobs.

Scope note: state lives in the process. The API runs a single uvicorn worker
(see Dockerfile), so one registry serves every request; a redeploy drops
in-flight jobs, which is acceptable for an admin-triggered utility. Bedrock
spend is still durably recorded in ``translation_usage`` as each chunk lands,
so a lost job never loses its cost accounting.

Producers (the job runner) append events; consumers (SSE streams) replay the
backlog then wait on ``updated``. Everything runs on the event loop — blocking
work is pushed to threads by the caller — so no locking is needed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Finished jobs linger so a client that reconnects can still read the outcome.
_JOB_TTL = timedelta(minutes=30)

# Hard cap on retained jobs — a backstop against unbounded growth if TTL sweeps
# somehow never run (e.g. no new jobs are ever started).
_MAX_JOBS = 100


@dataclass
class VideoCcJob:
    """One caption-translation run: its event log and terminal state."""

    id: str
    video_ref: str
    status: str = "running"  # running | completed | failed
    events: list[dict[str, Any]] = field(default_factory=list)
    updated: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    def emit(self, event_type: str, **payload: Any) -> None:
        """Append an event and wake every attached SSE stream."""
        self.events.append({"type": event_type, **payload})
        self.updated.set()

    def finish(self, status: str) -> None:
        """Mark the job terminal so streams can close."""
        self.status = status
        self.finished_at = datetime.now(timezone.utc)
        self.updated.set()

    @property
    def is_done(self) -> bool:
        return self.status != "running"


_jobs: dict[str, VideoCcJob] = {}


def create_job(video_ref: str) -> VideoCcJob:
    """Register a new job and sweep expired ones."""
    _sweep()
    job = VideoCcJob(id=uuid.uuid4().hex, video_ref=video_ref)
    _jobs[job.id] = job
    return job


def get_job(job_id: str) -> VideoCcJob | None:
    return _jobs.get(job_id)


def _sweep() -> None:
    """Drop finished jobs past their TTL, then trim to the retention cap."""
    cutoff = datetime.now(timezone.utc) - _JOB_TTL
    for job_id in [
        j.id for j in _jobs.values() if j.finished_at and j.finished_at < cutoff
    ]:
        _jobs.pop(job_id, None)

    if len(_jobs) > _MAX_JOBS:
        # Oldest-first; never evict a job that is still running.
        evictable = sorted(
            (j for j in _jobs.values() if j.is_done), key=lambda j: j.created_at
        )
        for job in evictable[: len(_jobs) - _MAX_JOBS]:
            _jobs.pop(job.id, None)


async def watch(job: VideoCcJob, heartbeat: float = 15.0):
    """Yield the job's events, backlog first, then live until it finishes.

    Args:
        heartbeat: seconds to wait before yielding ``None``, which the caller
            turns into an SSE comment so proxies don't idle out the connection.
    """
    cursor = 0
    while True:
        while cursor < len(job.events):
            yield job.events[cursor]
            cursor += 1

        if job.is_done:
            return

        job.updated.clear()
        # Re-check after clearing: an event appended between the drain above and
        # the clear would otherwise leave us waiting on an already-consumed flag.
        if cursor < len(job.events) or job.is_done:
            continue
        try:
            await asyncio.wait_for(job.updated.wait(), timeout=heartbeat)
        except asyncio.TimeoutError:
            yield None
