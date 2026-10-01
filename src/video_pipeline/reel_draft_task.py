"""One model turn on a reel draft, run after the request that asked for it has returned.

A turn is a Sonnet call over the whole transcript, retried once when its answer
breaks a rule, so it can outlast the load balancer's 60 second idle timeout.
The router hands it to FastAPI's ``BackgroundTasks`` instead, and the admin
screen polls while the reel is `drafting`.

The first turn cuts the reel (``trailer_select``); every later one revises it
as the admin's latest message asks (``trailer_revise``). A turn that fails puts
the draft back as it was, with the reason in `error`, so nothing the admin has
reviewed is lost. The turn opens its own session: the request's is closed by
the time it runs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.db.base import get_session_factory
from src.video_pipeline import reel_sources
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_draft import message
from src.video_pipeline.reel_models import DRAFT, DRAFTING, WebinarVideoReel
from src.video_pipeline.trailer_revise import revise_segments
from src.video_pipeline.trailer_select import Selection, select_segments
from src.video_pipeline.trailer_sentences import split_sentences

logger = logging.getLogger(__name__)


def _finish(db, reel_id: uuid.UUID, started: datetime, **values: Any) -> bool:
    """Write the turn's result only while the row is as the turn found it.

    A draft discarded mid-turn is gone, and one the list endpoint gave up on
    (``reel_service.DRAFTING_STALE_AFTER``) may be `drafting` again for a newer
    request — `updated_at` moved on either way, so this turn's stale result
    cannot overwrite that newer message.
    """
    # Locked and re-read rather than a conditional UPDATE: `updated_at` is only
    # compared reliably once it has come back through the column's own type.
    reel = db.get(WebinarVideoReel, reel_id, with_for_update=True, populate_existing=True)
    if reel is None or reel.state != DRAFTING or reel.updated_at != started:
        db.rollback()
        return False
    for key, value in {"state": DRAFT, **values}.items():
        setattr(reel, key, value)
    db.commit()
    return True


def first_cut_reply(selection: Selection) -> str:
    count = len(selection.segments)
    return (f"Here's a first cut: {count} clip{'s' if count != 1 else ''}, "
            f"{selection.total_seconds:.0f}s. Tell me what to change, or render it.")


def _turn(reel: WebinarVideoReel, job: WebinarVideoJob) -> tuple[Selection, str]:
    inputs = reel_sources.load_inputs(job)
    sentences = split_sentences(inputs.cues)
    messages = list(reel.messages or [])
    if not reel.selection:
        # No cut yet (the first turn, or a retry after it failed): everything the
        # admin has asked for so far is the direction.
        asks = [m["text"] for m in messages if m.get("role") == "admin"]
        selection = select_segments(sentences, inputs.chapters, inputs.title,
                                    focus="\n".join(asks) or None)
        return selection, first_cut_reply(selection)
    request = messages[-1]["text"] if messages and messages[-1].get("role") == "admin" else ""
    return revise_segments(sentences, inputs.chapters, inputs.title,
                           current=Selection.from_dict(reel.selection),
                           conversation=messages[:-1], request=request, focus=reel.prompt)


def run_turn(reel_id: uuid.UUID) -> None:
    """Cut or revise the draft. Never raises: a failure is written to the row."""
    db = get_session_factory()()
    started = None
    try:
        reel = db.get(WebinarVideoReel, reel_id)
        if reel is None or reel.state != DRAFTING:
            logger.info("Reel %s is not drafting — nothing to do", reel_id)
            return
        started = reel.updated_at
        job = db.get(WebinarVideoJob, reel.job_id)
        selection, reply = _turn(reel, job)
        if not _finish(db, reel_id, started, selection=selection.as_dict(),
                       hook_title=selection.hook_title, error=None,
                       duration_seconds=Decimal(str(round(selection.total_seconds, 2))),
                       messages=[*(reel.messages or []), message("assistant", reply)]):
            logger.info("Reel %s left drafting mid-turn — result dropped", reel_id)
    except Exception as exc:
        logger.exception("Reel %s draft turn failed", reel_id)
        try:
            db.rollback()
            if started is not None:
                _finish(db, reel_id, started, error=f"Couldn't make that change — {exc}"[:2000])
        except Exception:
            logger.exception("Could not record the failure for reel %s", reel_id)
    finally:
        db.close()
