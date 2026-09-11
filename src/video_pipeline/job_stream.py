"""Live progress for the monitoring screen, as server-sent events.

Same wire protocol as the Video CC stream in ``src/content/video_cc_router.py``
— ``data:`` frames, ``:`` comment frames as heartbeats — but a different source.
Video CC's progress lives in the process that is doing the work, so it can be
awaited. Pipeline progress is written by an ECS task and the sweeper, so the
only shared truth is the job row: these endpoints poll it and emit whenever the
answer changes.

Three things shape the implementation:

* **The polling query is synchronous.** The API runs one uvicorn worker, so a
  blocking query inside a stream would stall every other request. Each poll goes
  through ``asyncio.to_thread`` on its own short-lived session.
* **Heartbeats are not optional.** The ALB sets no ``idle_timeout``, so it uses
  the AWS default of 60 seconds and would drop a quiet stream. Comment frames go
  out well inside that.
* **Every connect starts with a full snapshot.** A page reload reopens the
  stream, and a client that had to wait for the next change to render anything
  would show an empty stepper until something moved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from src.auth.deps import AdminDep
from src.db.base import get_session_factory
from src.db.deps import DbDep
from src.video_pipeline import job_views
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import ACTIVE_STATES
from src.workshops.models import Webinar

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/video-pipeline", tags=["video-pipeline-admin"])

# A stage boundary is the fastest thing worth seeing, and stages last tens of
# seconds at best, so two seconds is already finer than the data changes.
ACTIVE_POLL_SECONDS = 2.0
# A settled job only changes if someone retries it from this very page. Worth
# staying connected for, not worth polling hard for.
SETTLED_POLL_SECONDS = 10.0
HEARTBEAT_SECONDS = 15.0
# Connections are recycled rather than held open indefinitely; the client
# reopens, and reopening is a full snapshot anyway.
MAX_STREAM_SECONDS = 900.0
# Enough for every job the screen can show at once without streaming the archive.
ACTIVE_LIMIT = 50

_UNSET = object()


def _jsonable(model: Any) -> Any:
    """Pydantic model to plain JSON types, via its own serializers."""
    return json.loads(model.model_dump_json())


def _load_detail(job_id: uuid.UUID) -> dict[str, Any]:
    """One job's full detail, or a `missing` frame if it is gone."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        job = db.execute(
            select(WebinarVideoJob)
            .options(joinedload(WebinarVideoJob.webinar).joinedload(Webinar.workshop))
            .where(WebinarVideoJob.id == job_id)
        ).scalar_one_or_none()
        if job is None:
            return {"type": "missing"}
        return {"type": "job", "job": _jsonable(job_views.to_detail(job))}
    finally:
        db.close()


def _load_active() -> dict[str, Any]:
    """Summaries of every job still in flight, newest first."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        jobs = (
            db.execute(
                select(WebinarVideoJob)
                .options(joinedload(WebinarVideoJob.webinar).joinedload(Webinar.workshop))
                .where(WebinarVideoJob.state.in_([s.value for s in ACTIVE_STATES]))
                .order_by(WebinarVideoJob.created_at.desc())
                .limit(ACTIVE_LIMIT)
            )
            .scalars()
            .all()
        )
        return {"type": "jobs", "jobs": [_jsonable(job_views.to_summary(job)) for job in jobs]}
    finally:
        db.close()


def _detail_interval(payload: dict[str, Any]) -> float:
    job = payload.get("job") or {}
    return ACTIVE_POLL_SECONDS if job.get("state") in {s.value for s in ACTIVE_STATES} else SETTLED_POLL_SECONDS


async def _frames(
    load: Callable[[], dict[str, Any]], interval: Callable[[dict[str, Any]], float]
):
    """Yield SSE frames: a snapshot on every change, a comment when quiet."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_STREAM_SECONDS
    last: Any = _UNSET
    last_frame_at = loop.time()

    while loop.time() < deadline:
        payload = await asyncio.to_thread(load)
        if payload != last:
            last = payload
            last_frame_at = loop.time()
            yield f"data: {json.dumps(payload)}\n\n"
            if payload.get("type") == "missing":
                return
        elif loop.time() - last_frame_at >= HEARTBEAT_SECONDS:
            last_frame_at = loop.time()
            yield ": keep-alive\n\n"
        await asyncio.sleep(interval(payload))


def _response(
    label: str,
    load: Callable[[], dict[str, Any]],
    interval: Callable[[dict[str, Any]], float],
) -> StreamingResponse:
    async def event_stream():
        try:
            async for frame in _frames(load, interval):
                yield frame
        except asyncio.CancelledError:
            logger.debug("video_pipeline: %s stream closed by client", label)
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _release(db: DbDep) -> None:
    """Hand the request's DB connection back before streaming for minutes.

    The auth dependency queries through this session, which leaves a transaction
    open, and a session's teardown does not run until the response has finished
    — which for a stream is a quarter of an hour. Closing it here keeps one
    pooled connection per open admin tab from being the cost of watching a job.
    """
    db.close()


@router.get("/jobs/active/stream")
async def stream_active_jobs(_admin: AdminDep, db: DbDep) -> StreamingResponse:
    """Every in-flight job, re-sent whenever any of them moves.

    Three segments, so this cannot be mistaken for a job whose id is "active".
    """
    _release(db)
    return _response("active-jobs", _load_active, lambda _payload: ACTIVE_POLL_SECONDS)


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> StreamingResponse:
    """One job's detail, re-sent on every change until the client goes away."""
    _release(db)
    return _response(f"job {job_id}", lambda: _load_detail(job_id), _detail_interval)
