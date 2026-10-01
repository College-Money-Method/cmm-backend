"""Revise a trailer reel's clip selection in conversation with an admin.

The first cut comes from ``trailer_select``. An admin then reads the merged
transcript and asks for changes ("swap clip 2 for the FAFSA deadline part",
"a punchier hook"). Each request goes back to Sonnet with the whole transcript,
the reel as it stands and the conversation so far, and the answer is held to
the same rules as the first cut — ``validate`` — so a revision cannot produce a
reel the first cut would have been refused for.

The model also answers the admin in a sentence or two ("reply"): what it
changed, or which rule stopped it doing what was asked.
"""

from __future__ import annotations

from typing import Any

from src.video_pipeline import bedrock_usage
from src.video_pipeline.trailer_select import (
    SYSTEM_PROMPT,
    Selection,
    SelectionError,
    ask_model,
    build_prompt,
    presenter_allowed,
)
from src.video_pipeline.transcript import Cue, format_timestamp

REVISE_PROMPT = SYSTEM_PROMPT + """

You are now revising a reel together with an admin who has read its transcript. You get the \
reel as it stands (its clips numbered in play order) and the conversation so far. Make the \
change the admin asks for and keep every clip they did not ask to change, in the same order. \
Every rule above still applies; when a request would break one, do the closest thing the \
rules allow and say so. Lines marked "Admin edited the reel" are changes the admin made by \
hand: do not undo them unless asked.
Reply with the same JSON object plus one more key, "reply": one or two plain sentences to \
the admin saying what you changed, or why you could not."""

# Who said each conversation line, as the model reads it.
_SPEAKERS = {"admin": "Admin", "assistant": "You", "edit": "Admin edited the reel"}


def describe_reel(selection: Selection) -> str:
    """The reel as the model reads it: hook title, then each clip's range and words."""
    clips = "\n".join(
        f"Clip {n}: sentences [{s.first_cue}]-[{s.last_cue}], starts "
        f"{format_timestamp(s.start)}, {s.duration:.1f}s\n  {s.text}"
        for n, s in enumerate(selection.segments, start=1)
    )
    return (f"Hook title: {selection.hook_title}\n{clips}\n"
            f"Total: {selection.total_seconds:.1f}s")


def revise_segments(
    cues: list[Cue],
    chapters: list[dict[str, Any]],
    title: str,
    *,
    current: Selection,
    conversation: list[dict[str, Any]],
    request: str,
    focus: str | None = None,
    presenter: str | None = None,
    attempts: int = 2,
) -> tuple[Selection, str]:
    """The reel changed as `request` asks, and the model's reply to the admin.

    `conversation` is the stored message list before `request`, oldest first:
    ``{"role": "admin" | "assistant" | "edit", "text": ...}``. `focus` is the
    direction the reel was first asked for with, kept in view for every turn.
    """
    if not cues:
        raise SelectionError("no transcript cues — nothing to choose from")
    allowed = presenter_allowed(cues, presenter)
    history = "\n".join(
        f"{_SPEAKERS.get(m.get('role'), 'Admin')}: {m.get('text', '')}" for m in conversation
    )
    prompt = (
        f"{build_prompt(cues, chapters, title, allowed, focus)}\n\n"
        f"The reel as it stands:\n{describe_reel(current)}\n\n"
        f"Conversation so far:\n{history or '(none)'}\n\n"
        f"The admin now asks: {request.strip()}"
    )
    selection, data = ask_model(REVISE_PROMPT, prompt, cues, allowed,
                                bedrock_usage.TRAILER_REVISE, attempts)
    reply = str(data.get("reply") or "").strip() or "Updated the reel."
    return selection, reply[:1000]
