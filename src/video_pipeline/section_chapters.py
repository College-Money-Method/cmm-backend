"""Build the chapter list from transcript sections, named by the frames.

The counterpart to `chapter_build`, which segments a recording from its frames
alone. Here the transcript has already decided where the sections are (see
`topic_segment`) and the frames are consulted only inside a window around each
boundary, for two things the transcript cannot supply: the deck's own wording
for the title, and a timecode that lands on the slide rather than on the
sentence that introduced it.

That narrowing is the point. Every heading-only slide used to become a chapter,
so a bio slide and an agenda in the opening minutes produced three chapters
before the session had begun. A card now only titles a section the speaker
actually started.
"""

from __future__ import annotations

import logging

from src.config import settings
from src.video_pipeline.chapter_build import (
    LABEL_INTRODUCTION,
    LABEL_QNA,
    LABEL_TOUR,
    Chapter,
    apply_cap,
    dedupe,
    find_qna_start,
    normalise_heading,
)
from src.video_pipeline.frame_classify import TITLE_CARD, Classified
from src.video_pipeline.topic_segment import INTRODUCTION, QNA, TOUR, Section
from src.video_pipeline.transcript import Cue, format_timestamp

logger = logging.getLogger(__name__)

WINDOW_BEFORE = settings.video_topic_window_before_seconds
WINDOW_AFTER = settings.video_topic_window_after_seconds

# The recurring segments keep their configured labels whatever the model called
# them, so a school's page reads the same every week.
_FIXED_LABELS = {
    INTRODUCTION: (LABEL_INTRODUCTION, "intro"),
    TOUR: (LABEL_TOUR, "tour"),
    QNA: (LABEL_QNA, "qna"),
}

# How far past a Q&A section's own start the transcript may be searched for the
# sentence that opens it. The topic pass reads merged blocks, so its answer is
# only accurate to a block; the phrase itself is exact.
_QNA_SNAP_SECONDS = 120.0


def windows(
    sections: list[Section],
    *,
    before: float = WINDOW_BEFORE,
    after: float = WINDOW_AFTER,
) -> list[tuple[float, float]]:
    """The spans of the recording worth inspecting frame by frame."""
    return [(max(0.0, s.start - before), s.start + after) for s in sections]


def in_any_window(timestamp: float, spans: list[tuple[float, float]]) -> bool:
    return any(low <= timestamp <= high for low, high in spans)


def _card_for(
    section: Section, frames: list[Classified], *, before: float, after: float
) -> Classified | None:
    """The title card that opens ``section``, if the frames show one.

    Nearest to the boundary rather than first in the window: a long look-back
    can reach the tail of the previous section, and the card that opens this one
    is the one closest to where the speaker turned.
    """
    low, high = max(0.0, section.start - before), section.start + after
    cards = [
        f
        for f in frames
        if f.type == TITLE_CARD and f.heading.strip() and low <= f.timestamp <= high
    ]
    if not cards:
        return None
    nearest = min(cards, key=lambda f: abs(f.timestamp - section.start))

    # Walk back over earlier frames of the same card. The sampler emits one
    # frame per visual change, so a card re-read after a camera cut appears
    # twice and the later copy is not where the section started.
    same = [
        f
        for f in cards
        if normalise_heading(f.heading) == normalise_heading(nearest.heading)
    ]
    return min(same, key=lambda f: f.timestamp)


def _qna_timecode(section: Section, cues: list[Cue]) -> float:
    """Snap a Q&A section onto the sentence that opens it, when there is one."""
    spoken = find_qna_start(cues, section.start - WINDOW_BEFORE)
    if spoken is None or abs(spoken - section.start) > _QNA_SNAP_SECONDS:
        return section.start
    return spoken


def build_from_sections(
    sections: list[Section],
    frames: list[Classified],
    *,
    cues: list[Cue] | None = None,
    max_chapters: int = settings.video_max_chapters,
    window_before: float = WINDOW_BEFORE,
    window_after: float = WINDOW_AFTER,
) -> list[Chapter]:
    """One chapter per section, titled from the deck wherever the deck says so."""
    chapters: list[Chapter] = []
    for section in sections:
        fixed = _FIXED_LABELS.get(section.kind)
        if fixed is not None:
            label, source = fixed
            start = _qna_timecode(section, cues or []) if section.kind == QNA else section.start
            chapters.append(Chapter(int(start), label, source))
            continue

        card = _card_for(section, frames, before=window_before, after=window_after)
        if card is not None:
            chapters.append(Chapter(int(card.timestamp), card.heading.strip(), TITLE_CARD))
            continue

        # No slide titles this boundary. The model's own wording is the fallback,
        # and a section it could not name is one we cannot show.
        if not section.label:
            logger.warning(
                "section at %s has neither a title card nor a label; skipped",
                format_timestamp(section.start),
            )
            continue
        logger.info(
            "no title card within the window at %s — using the transcript label %r",
            format_timestamp(section.start),
            section.label,
        )
        chapters.append(Chapter(int(section.start), section.label, "topic"))

    chapters.sort(key=lambda chapter: chapter.timecode)
    chapters = dedupe(chapters)

    # Vimeo expects the timeline to start at zero, and the first section is the
    # opening one whatever the transcript said about where speech began.
    if chapters and chapters[0].timecode != 0:
        first = chapters[0]
        chapters[0] = Chapter(0, first.title, first.source)

    capped, _ = apply_cap(chapters, max_chapters)
    return capped


__all__ = ["build_from_sections", "in_any_window", "windows"]
