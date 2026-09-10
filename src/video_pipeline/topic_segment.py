"""Find a webinar's real sections by reading the transcript.

The slide deck is not a table of contents. A typical session opens with a title
slide, a presenter bio and an agenda inside its first four minutes, and drops a
heading-only slide mid-section whenever the presenter changes emphasis —
"Investment and Value" two minutes into "Preparing a college financial plan".
Promoting every one of those cards produced eleven chapters where a viewer
wanted seven, and no property of the frame separates the two cases: the sub-slide
and the section divider are the same red rectangle with the same white heading.

What separates them is what the speaker is doing. A section starts when the topic
turns; a sub-heading lands mid-explanation. So the transcript decides *where* the
sections are, and the frames are consulted only around those boundaries, to read
the deck's own wording for a title.

Every guard fails towards an empty list, which the caller reads as "segment from
the frames as before". A wrong section list is worse than no section list: it
deletes chapters that the frames found correctly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from src.config import settings
from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.transcript import Cue, format_timestamp

logger = logging.getLogger(__name__)

# What a section is called when it is one of the recurring segments. These map
# onto the fixed labels in `chapter_build`, so a tour is called the same thing
# every week no matter how the presenter announced it.
CONTENT = "content"
INTRODUCTION = "introduction"
TOUR = "tour"
QNA = "qna"

VALID_KINDS = frozenset({CONTENT, INTRODUCTION, TOUR, QNA})

BLOCK_SECONDS = settings.video_topic_block_seconds
MIN_SECTION_SECONDS = settings.video_min_section_seconds
MAX_SECTIONS = settings.video_max_sections

_SYSTEM = """\
You are given the transcript of a recorded webinar for families about college \
financial aid, as numbered blocks with timestamps.

Divide it into the sections a viewer would want in a chapter menu. Aim for the \
handful of places where the topic genuinely turns — an hour-long session usually \
has four to eight. Judge by what the speaker is doing, not by what is on screen.

A new section starts where the speaker finishes one topic and takes up another: \
summarising and moving on, announcing what comes next, or opening a subject that \
has not been discussed yet.

A new section does NOT start at:
- a sub-point, example, or story inside the topic being explained
- a restatement or a change of emphasis on the same topic
- an aside, a question from the audience, or a technical interruption

Also mark these recurring segments when they are present, using "kind":
- "introduction": the opening — greeting, who the presenter is, what the session \
will cover. Everything before the first real topic is one introduction.
- "tour": a walkthrough of a website, portal, or resource centre.
- "qna": the point where the presenter stops presenting and starts taking \
questions.
Everything else is "content".

Reply with only a JSON object:
{"sections": [{"block": <integer>, "kind": "<content|introduction|tour|qna>", \
"label": "<short title>"}]}

Rules:
- block must be one of the numbers shown.
- List sections in ascending block order, starting with the introduction if there \
is one.
- Prefer too few sections over too many. A viewer scrubbing a menu of twenty \
entries has been given a worse recording, not a better one.
- Keep each label under 50 characters.\
"""


@dataclass(frozen=True)
class Section:
    """One topic boundary the transcript supports."""

    start: float   # seconds on the trimmed video's clock
    kind: str      # content | introduction | tour | qna
    label: str     # the model's wording, used only when no title card is found

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class Block:
    """Consecutive cues merged into one numbered line of the prompt."""

    start: float
    text: str


def merge_blocks(cues: list[Cue], block_seconds: float = BLOCK_SECONDS) -> list[Block]:
    """Merge cues into blocks of roughly ``block_seconds``.

    Zoom emits a cue per sentence, so a long session is well over a thousand of
    them and a per-cue prompt buries the structure it is asking about. The
    resolution given up here is not needed: the frame window searched around a
    boundary is wider than a block, and the title card sets the final timecode.
    """
    blocks: list[Block] = []
    start: float | None = None
    parts: list[str] = []
    for cue in cues:
        if start is None:
            start = cue.start
        parts.append(cue.text)
        if cue.end - start >= block_seconds:
            blocks.append(Block(start, " ".join(parts)))
            start, parts = None, []
    if parts and start is not None:
        blocks.append(Block(start, " ".join(parts)))
    return blocks


def build_prompt(blocks: list[Block]) -> str:
    """Render blocks as a numbered, timestamped list."""
    return "\n".join(
        f"[{i}] {format_timestamp(block.start)} {block.text}"
        for i, block in enumerate(blocks)
    )


def _parse(raw: list, blocks: list[Block]) -> list[Section]:
    """Turn the model's replies into sections, dropping anything unusable.

    Each entry has to name a block that exists. That is what keeps a boundary on
    a moment the transcript actually contains, rather than on a timestamp the
    model composed.
    """
    sections: list[Section] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        index = entry.get("block")
        if not isinstance(index, int) or isinstance(index, bool):
            logger.warning("section entry has no integer block: %r", entry)
            continue
        if not 0 <= index < len(blocks):
            logger.warning("section block %d outside 0..%d", index, len(blocks) - 1)
            continue
        kind = str(entry.get("kind") or CONTENT).strip().lower()
        if kind not in VALID_KINDS:
            kind = CONTENT
        sections.append(
            Section(
                start=blocks[index].start,
                kind=kind,
                label=str(entry.get("label") or "").strip(),
            )
        )
    return sections


def _thin(sections: list[Section], min_seconds: float) -> list[Section]:
    """Enforce ascending order, one of each recurring kind, and a minimum gap.

    The recurring segments happen once per webinar — that is what makes them
    recurring, and why they carry a fixed title. The model does not always
    honour that: a session that demonstrates two parts of the product in turn
    comes back with two `tour` sections, both of which would be titled
    "Resource center tour" and the second of which would then be deduped away,
    losing a real boundary. A repeat is demoted to content instead, so it keeps
    its own wording and survives.

    Content sections are held apart by ``min_seconds``; the recurring ones are
    exempt. A tour that runs straight into the Q&A is two real chapters two
    minutes apart, and dropping one to satisfy a spacing rule would lose the
    boundary a viewer most wants.
    """
    kept: list[Section] = []
    seen: set[str] = set()
    for section in sorted(sections, key=lambda s: s.start):
        if section.kind != CONTENT:
            if section.kind in seen:
                logger.info(
                    "demoting a second %r section at %s to content",
                    section.kind,
                    format_timestamp(section.start),
                )
                section = replace(section, kind=CONTENT)
            else:
                seen.add(section.kind)
        if not kept:
            kept.append(section)
            continue
        previous = kept[-1]
        recurring = section.kind != CONTENT or previous.kind != CONTENT
        if not recurring and section.start - previous.start < min_seconds:
            logger.info(
                "dropping section %r at %s — %.0fs after the previous one",
                section.label,
                format_timestamp(section.start),
                section.start - previous.start,
            )
            continue
        kept.append(section)
    return kept


def detect_sections(
    cues: list[Cue],
    *,
    min_seconds: float = MIN_SECTION_SECONDS,
    max_sections: int = MAX_SECTIONS,
) -> list[Section]:
    """Section boundaries the transcript supports, or ``[]`` to fall back.

    Returning an empty list is a normal outcome, not an error: no transcript, a
    failed call, or a reply that does not survive the guards all mean the frames
    have to segment the recording on their own, as they did before this pass
    existed.
    """
    if not cues:
        return []

    blocks = merge_blocks(cues)
    if not blocks:
        return []

    try:
        parsed, in_tok, out_tok = call_json(
            system=_SYSTEM, content=build_prompt(blocks), max_tokens=2000
        )
    except BedrockCallError as exc:
        logger.warning("topic segmentation failed, chaptering from frames alone: %s", exc)
        return []

    raw = parsed.get("sections")
    if not isinstance(raw, list) or not raw:
        logger.warning("topic segmentation reply had no sections: %r", parsed)
        return []

    sections = _thin(_parse(raw, blocks), min_seconds)
    if not sections:
        logger.warning("no section survived the guards; chaptering from frames alone")
        return []
    if len(sections) > max_sections:
        # Not a granular answer — a broken one. Trusting it would delete the
        # frames' own reading of the deck and replace it with noise.
        logger.warning(
            "topic segmentation returned %d sections, over the %d ceiling — ignoring",
            len(sections),
            max_sections,
        )
        return []

    logger.info(
        "topic segmentation: %d sections from %d blocks (%d in / %d out tokens) — %s",
        len(sections),
        len(blocks),
        in_tok,
        out_tok,
        ", ".join(f"{format_timestamp(s.start)} {s.kind}:{s.label}" for s in sections),
    )
    return sections


__all__ = [
    "CONTENT",
    "INTRODUCTION",
    "QNA",
    "TOUR",
    "Block",
    "Section",
    "build_prompt",
    "detect_sections",
    "merge_blocks",
]
