"""Choose the moments of a webinar that make a ~60 second trailer reel.

The model picks, the code decides. Sonnet is shown the transcript as numbered
timed lines — sentences, from ``trailer_sentences`` — and answers with *index
ranges*, never timestamps: an index can only name a line that exists, so every
cut lands on a line boundary and the reel says what the transcript says. Times,
padding and every length rule are applied here, and an answer that leaves too
little to make a reel goes back to the model once with the reasons.

The reel shows the presenter's camera only (Zoom's ``active_speaker`` file), so
the prompt steers away from moments that only make sense with the slide in view.
It is the presenter's voice only, too: a segment in which anyone else speaks is
dropped here, whatever the model answered.

Sonnet rather than the pipeline's usual Haiku: this is the one call that reads
the whole transcript for judgement, and Haiku kept picking lines that lean on a
slide. One call a webinar, so the price difference is cents.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.trailer_sentences import speakers
from src.video_pipeline.transcript import Cue, format_timestamp

logger = logging.getLogger(__name__)

TOTAL_MIN_SECONDS = 45.0
TOTAL_MAX_SECONDS = 65.0
SEGMENT_MIN_SECONDS = 5.0
SEGMENT_MAX_SECONDS = 25.0
MIN_SEGMENTS = 3
MAX_SEGMENTS = 6
# The model may propose more than the reel keeps; the extras are the spares
# `validate` falls back on when an earlier pick is the wrong length.
MAX_CANDIDATES = 8
# Breathing room either side of a cut, so the first and last words are not
# clipped. Never reaches into a neighbouring cue.
PAD_SECONDS = 0.15

SYSTEM_PROMPT = f"""You edit webinar recordings into short social-media trailer reels for \
College Money Method, which teaches families how to pay for college.

You get a webinar's transcript as numbered sentences ("[index] m:ss +<seconds>s text": start \
time, how long the sentence lasts, what was said) and its chapter list. A speaker's name \
starts the first sentence of each of their turns. \
Pick the moments that make the best ~60 second trailer: a reel that makes a parent want to \
watch the full webinar.

Rules:
- Propose 4 to {MAX_CANDIDATES} segments in the order they should play; the reel keeps \
{MIN_SEGMENTS}-{MAX_SEGMENTS} of them, dropping any that are the wrong length or that no longer \
fit the total, so put the best first and the spares last. Each is a run of consecutive sentences, \
{SEGMENT_MIN_SECONDS:.0f}-{SEGMENT_MAX_SECONDS:.0f} seconds long. \
All segments together must total {TOTAL_MIN_SECONDS:.0f}-{TOTAL_MAX_SECONDS:.0f} seconds. \
A segment is 1 to 4 sentences. A longer one is cut short after its last sentence that \
fits {SEGMENT_MAX_SECONDS:.0f}s, so open each segment with its strongest line.
- Each segment must make sense on its own: start at the beginning of a sentence and end at \
the end of one. No "as I said", "this one here", or anything that needs earlier context.
- The video shows only the presenter's face, never the slides. Skip moments that describe \
what is on screen ("as you can see", "click here", "this chart").
- Prefer a strong hook first (a surprising fact, a common costly mistake, a clear promise), \
then concrete, useful advice, and end on a line that makes people want the full session.
- Only the sentences marked "(presenter)" may be picked, and a segment must not include \
any other speaker's sentence. Skip greetings, housekeeping, audio checks, attendee names and \
answers that only make sense after hearing the question.
- Do not pick a claim that becomes misleading when cut short (a dollar amount or a \
guarantee stripped of its conditions). If a strong segment mentions dollar amounts, \
guarantees or specific schools, keep it only if it stands alone and flag it.
- Segments must not overlap.

Reply with only this JSON object:
first_cue and last_cue are sentence indexes, both included.
{{"hook_title": "<on-screen title, max 8 words, no hashtags or emoji>",
  "segments": [{{"first_cue": <int>, "last_cue": <int>, "why": "<max 10 words>",
                 "flags": ["dollar_amount" | "guarantee" | "school_name" | "needs_context"]}}]}}"""


class SelectionError(RuntimeError):
    """The model's answer broke a rule, or no usable answer came back."""


@dataclass(frozen=True)
class Segment:
    """One cut of the reel, on the clock of the transcript it was chosen from."""

    first_cue: int
    last_cue: int
    start: float
    end: float
    text: str
    why: str = ""
    flags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Selection:
    hook_title: str
    segments: list[Segment]

    @property
    def total_seconds(self) -> float:
        return sum(segment.duration for segment in self.segments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hook_title": self.hook_title,
            "total_seconds": round(self.total_seconds, 2),
            "segments": [asdict(segment) for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Selection:
        return cls(
            hook_title=str(data["hook_title"]),
            segments=[Segment(**item) for item in data["segments"]],
        )


def build_prompt(
    cues: list[Cue],
    chapters: list[dict[str, Any]],
    title: str,
    allowed: list[bool],
    focus: str | None = None,
) -> str:
    """The user message: title, chapters, then every cue on its own numbered line.

    Presenter lines are marked. The others stay in, so the model can see what a
    presenter answer is answering, but cannot be picked. `focus` is an admin's
    editorial direction ("Focus on merit aid"): it steers which moments are
    picked, never the rules the answer is validated against.
    """
    direction = (
        f"Editorial direction for this reel: {focus.strip()}\n"
        "Prefer moments that serve it; every rule still applies.\n\n"
        if focus and focus.strip()
        else ""
    )
    chapter_lines = "\n".join(
        f"- {format_timestamp(float(ch['timecode']))} {ch['title']}" for ch in chapters
    )
    cue_lines = "\n".join(
        f"[{i}] {format_timestamp(cue.start)} +{cue.end - cue.start:.0f}s"
        f"{' (presenter)' if allowed[i] else ''} {cue.text}"
        for i, cue in enumerate(cues)
    )
    return (
        f"{direction}Webinar title: {title}\n\nChapters:\n{chapter_lines or '- (none)'}\n\n"
        f"Transcript:\n{cue_lines}"
    )


def select_segments(
    cues: list[Cue],
    chapters: list[dict[str, Any]],
    title: str,
    *,
    presenter: str | None = None,
    focus: str | None = None,
    attempts: int = 2,
) -> Selection:
    """Ask the model for a reel, retrying once with the reason an answer was refused.

    Only `presenter`'s sentences (default: the configured presenter) can be used.
    `focus` is optional editorial direction, passed to the model as given.
    """
    if not cues:
        raise SelectionError("no transcript cues — nothing to choose from")
    allowed = presenter_mask(cues, presenter or settings.trailer_presenter_name)
    if not any(allowed):
        raise SelectionError(f"no sentences by {presenter or settings.trailer_presenter_name}")
    prompt = build_prompt(cues, chapters, title, allowed, focus)
    feedback = ""
    last_error = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            data, tokens_in, tokens_out = call_json(
                system=SYSTEM_PROMPT,
                content=prompt + feedback,
                invoke_type=bedrock_usage.TRAILER_SELECT,
                max_tokens=1500,
                model_id=settings.bedrock_sonnet_model_id,
            )
        except BedrockCallError as exc:
            last_error = str(exc)
            logger.warning("Trailer selection attempt %d failed: %s", attempt, exc)
            continue
        logger.info("Trailer selection attempt %d: %d in / %d out tokens",
                    attempt, tokens_in, tokens_out)
        try:
            return validate(data, cues, allowed)
        except SelectionError as exc:
            last_error = str(exc)
            logger.warning("Trailer selection attempt %d rejected: %s", attempt, exc)
            feedback = (
                f"\n\nYour previous answer was rejected: {exc}\n"
                "Answer again, fixing that, with the same JSON shape."
            )
    raise SelectionError(f"no usable reel after {attempts} attempts: {last_error}")


def presenter_mask(cues: list[Cue], presenter: str) -> list[bool]:
    """True for each sentence the presenter says.

    A transcript with no speaker labels at all is taken to be the presenter's.
    Unlabelled lines before the first label are not, since nobody can tell.
    """
    names = speakers(cues)
    if not any(names):
        return [True] * len(cues)
    wanted = presenter.strip().casefold()
    return [name.casefold().startswith(wanted) for name in names]


def validate(
    data: dict[str, Any], cues: list[Cue], allowed: list[bool] | None = None
) -> Selection:
    """Turn the model's cue ranges into a reel that fits every rule.

    The model proposes more segments than the reel needs; the arithmetic is done
    here, because a model adding up two dozen durations is the step it gets
    wrong. In play order, a segment is kept when it is the right length, does
    not overlap one already kept, and still fits the total — otherwise it is
    dropped with a reason, and the reasons go back to the model on a retry.
    A segment containing a sentence that is not `allowed` (someone other than
    the presenter) is dropped the same way.
    """
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise SelectionError("no segments in the answer")

    kept: list[Segment] = []
    notes: list[str] = []
    for n, item in enumerate(raw_segments[:MAX_CANDIDATES], start=1):
        try:
            first, last = int(item["first_cue"]), int(item["last_cue"])
        except (KeyError, TypeError, ValueError):
            notes.append(f"segment {n}: no integer first_cue/last_cue")
            continue
        if not 0 <= first <= last < len(cues):
            notes.append(f"segment {n}: cue range [{first}, {last}] is out of bounds")
            continue
        if allowed is not None and not all(allowed[first:last + 1]):
            notes.append(f"segment {n}: cues {first}-{last} include another speaker")
            continue
        why, flags = str(item.get("why") or ""), [str(f) for f in item.get("flags") or []]
        segment = _timed(cues, first, last, why, flags)
        while segment.duration > SEGMENT_MAX_SECONDS and segment.last_cue > first:
            # Too long: keep the leading sentences, so it still ends on one.
            segment = _timed(cues, first, segment.last_cue - 1, why, flags)
        if segment.last_cue < last:
            notes.append(f"segment {n}: shortened to cues {first}-{segment.last_cue}")
        label = f"segment {n} (cues {first}-{segment.last_cue}, {segment.duration:.1f}s)"
        if not SEGMENT_MIN_SECONDS <= segment.duration <= SEGMENT_MAX_SECONDS:
            notes.append(f"{label}: dropped, each must be "
                         f"{SEGMENT_MIN_SECONDS:.0f}-{SEGMENT_MAX_SECONDS:.0f}s")
        elif any(first <= k.last_cue and k.first_cue <= segment.last_cue for k in kept):
            notes.append(f"{label}: dropped, overlaps an earlier segment")
        elif len(kept) >= MAX_SEGMENTS:
            notes.append(f"{label}: dropped, the reel already has {MAX_SEGMENTS} segments")
        elif sum(k.duration for k in kept) + segment.duration > TOTAL_MAX_SECONDS:
            notes.append(f"{label}: dropped, would push the reel past {TOTAL_MAX_SECONDS:.0f}s")
        else:
            kept.append(segment)

    selection = Selection(
        hook_title=str(data.get("hook_title") or "").strip()[:80], segments=kept
    )
    if len(kept) < MIN_SEGMENTS or selection.total_seconds < TOTAL_MIN_SECONDS:
        notes.append(f"kept {len(kept)} segments totalling {selection.total_seconds:.1f}s; "
                     f"a reel needs at least {MIN_SEGMENTS} totalling "
                     f"{TOTAL_MIN_SECONDS:.0f}-{TOTAL_MAX_SECONDS:.0f}s")
        raise SelectionError("; ".join(notes))
    for note in notes:
        logger.info("Trailer selection: %s", note)
    return selection


def _timed(cues: list[Cue], first: int, last: int, why: str, flags: list[str]) -> Segment:
    """Pad a cue range outward without crossing into the neighbouring cues."""
    floor = cues[first - 1].end if first > 0 else 0.0
    ceiling = cues[last + 1].start if last + 1 < len(cues) else cues[last].end + PAD_SECONDS
    start = max(floor, cues[first].start - PAD_SECONDS)
    end = min(ceiling, cues[last].end + PAD_SECONDS)
    text = " ".join(cue.text for cue in cues[first:last + 1])
    return Segment(first_cue=first, last_cue=last, start=round(start, 3),
                   end=round(max(end, start), 3), text=text, why=why, flags=flags)
