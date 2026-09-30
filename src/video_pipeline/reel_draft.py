"""A reel's draft: the clip selection an admin reviews and refines before it renders.

    create_draft ─▶ drafting ─(model turn)─▶ draft ─▶ start_render ─▶ pending/rendering
                      ▲                        │
                      └──── add_message ◀──────┤ edit_draft (by hand, stays draft)

Each model turn — the first cut, or a revision the admin asked for — runs in
``reel_draft_task`` after the request has returned. Only one turn runs per
draft: a message is refused while the reel is `drafting`. Editing by hand
(reorder, remove, retitle) is instant and recorded in the conversation, so the
next model turn knows not to undo it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.video_pipeline import reel_sources, task_dispatch
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.reel_models import (
    ACTIVE_STATES,
    DRAFT,
    DRAFT_STATES,
    DRAFTING,
    ORIENTATIONS,
    PENDING,
    WebinarVideoReel,
)
from src.video_pipeline.reel_service import ALREADY_ACTIVE, ReelConflict, list_reels
from src.video_pipeline.trailer_select import Selection

NOT_A_DRAFT = "This reel has already been sent to render."
BUSY = "The last change is still being worked on — wait for it to finish."
# A conversation long enough to lose its thread; start a new draft instead.
MAX_MESSAGES = 60


def message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "text": text, "at": datetime.now(timezone.utc).isoformat()}


def _require_idle_draft(reel: WebinarVideoReel) -> None:
    if reel.state == DRAFTING:
        raise ReelConflict(BUSY)
    if reel.state != DRAFT:
        raise ReelConflict(NOT_A_DRAFT)


def _write_idle_draft(db: Session, reel: WebinarVideoReel, **values: Any) -> None:
    """Write `values` only while the reel is still an idle draft.

    Every change starts from the row as read, so two requests at once (two
    admins, a double click) could otherwise both pass the state check and the
    second overwrite the first's conversation. The UPDATE's own condition picks
    one winner; the loser re-reads the row and is refused for what it is now.
    """
    result = db.execute(
        update(WebinarVideoReel)
        .where(WebinarVideoReel.id == reel.id, WebinarVideoReel.state == DRAFT)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(reel)
    if result.rowcount != 1:
        _require_idle_draft(reel)
        raise ReelConflict(BUSY)  # pragma: no cover - changed and changed back


def create_draft(db: Session, job: WebinarVideoJob, prompt: str | None) -> WebinarVideoReel:
    """Insert a draft awaiting its first cut. The caller schedules the model turn."""
    reason = reel_sources.blocked_reason(job)
    if reason:
        raise ReelConflict(reason)
    direction = (prompt or "").strip() or None
    reel = WebinarVideoReel(job_id=job.id, prompt=direction, state=DRAFTING,
                            messages=[message("admin", direction)] if direction else [])
    db.add(reel)
    db.commit()
    return reel


def add_message(db: Session, reel: WebinarVideoReel, text: str) -> WebinarVideoReel:
    """Record the admin's request and hand the draft to a model turn."""
    _require_idle_draft(reel)
    messages = list(reel.messages or [])
    if len(messages) >= MAX_MESSAGES:
        raise ReelConflict("This conversation is long enough to lose its thread — "
                           "start a new reel instead.")
    _write_idle_draft(db, reel, messages=[*messages, message("admin", text.strip())],
                      state=DRAFTING, error=None)
    return reel


def edit_draft(db: Session, reel: WebinarVideoReel, *, hook_title: str | None,
               segment_order: list[int] | None) -> WebinarVideoReel:
    """Retitle, reorder or remove clips by hand. Raises ValueError on a bad order."""
    _require_idle_draft(reel)
    if not reel.selection:
        raise ReelConflict("There are no clips to edit yet.")
    current = Selection.from_dict(reel.selection)
    segments, notes = current.segments, []

    if segment_order is not None:
        if len(set(segment_order)) != len(segment_order) or not all(
                0 <= i < len(segments) for i in segment_order):
            raise ValueError("segment_order must name each current clip at most once")
        removed = [n for n in range(len(segments)) if n not in segment_order]
        notes += [f'removed clip {n + 1} ("{_opening(segments[n])}")' for n in removed]
        kept = sorted(segment_order)
        if segment_order != kept:
            notes.append("reordered the clips to " + ", ".join(
                str(i + 1) for i in segment_order) + " (by their old numbers)")
        segments = [segments[i] for i in segment_order]

    title = current.hook_title
    if hook_title is not None and not hook_title.strip():
        raise ValueError("A hook title cannot be blank")
    if hook_title is not None and hook_title.strip() != title:
        title = hook_title.strip()
        notes.append(f'changed the hook title to "{title}"')
    if not notes:
        return reel

    note = "; ".join(notes)
    note = note[0].upper() + note[1:]
    edited = Selection(hook_title=title, segments=segments)
    _write_idle_draft(db, reel, selection=edited.as_dict(), hook_title=title, error=None,
                      duration_seconds=Decimal(str(round(edited.total_seconds, 2))),
                      messages=[*(reel.messages or []), message("edit", note)])
    return reel


def start_render(db: Session, reel: WebinarVideoReel, job: WebinarVideoJob,
                 orientation: str) -> WebinarVideoReel:
    """Send the approved draft to the render task, one render at a time per job."""
    if orientation not in ORIENTATIONS:
        raise ValueError(f"Unknown orientation {orientation!r}")
    _require_idle_draft(reel)
    if not reel.selection or not reel.selection.get("segments"):
        raise ReelConflict("A reel needs at least one clip to render.")
    reason = reel_sources.blocked_reason(job)
    if reason:
        raise ReelConflict(reason)
    if any(r.state in ACTIVE_STATES for r in list_reels(db, job)):
        raise ReelConflict(ALREADY_ACTIVE)

    try:
        _write_idle_draft(db, reel, orientation=orientation, state=PENDING, error=None,
                          hook_title=reel.selection.get("hook_title"))
    except IntegrityError as exc:
        # Another request started this job's render since the check above.
        db.rollback()
        raise ReelConflict(ALREADY_ACTIVE) from exc
    task_dispatch.dispatch_reel(db, reel)
    db.refresh(reel)
    return reel


def discard(db: Session, reel: WebinarVideoReel) -> None:
    """Delete a draft. A model turn still running finds no row and writes nothing.

    Conditional on the row still being a draft, so a render another request
    started since this one read the row is never deleted out from under its task.
    """
    if reel.state not in DRAFT_STATES:
        raise ReelConflict("Only a draft can be discarded.")
    result = db.execute(
        delete(WebinarVideoReel)
        .where(WebinarVideoReel.id == reel.id, WebinarVideoReel.state.in_(DRAFT_STATES))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        raise ReelConflict("Only a draft can be discarded.")


def _opening(segment, words: int = 6) -> str:
    text = segment.lines[0]["text"] if segment.lines else segment.text
    head = text.split()
    return " ".join(head[:words]) + ("…" if len(head) > words else "")
