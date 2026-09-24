"""Build the chapter list from transcript sections and the deck's title cards.

The counterpart to `chapter_build`, which segments a recording from its frames
alone. Here both sources speak and the deck has the louder voice: a title card
is a slide the audience watched go up, so it supplies both the wording and a
timecode that lands on the slide rather than on the sentence that introduced it.
The transcript's sections fill in what the deck never titles — the opening, and
a tour or Q&A that runs on off the last slide.

Cards used to be read only inside a window around a transcript boundary, which
made the transcript the sole authority on where a section begins. It is not good
enough at that. A presenter who opens a topic a minute before advancing the
slide, or who introduces it in words nowhere near the deck's, moves the boundary
far enough that the card falls outside the window — and because the window also
decides which frames are downloaded, an unread card is never classified at all.
On a real recording that lost one chapter outright and left two others named
from the model's paraphrase and timed to the sentence rather than the slide.

What the window did protect against is still guarded, more narrowly: the title
slide of the session itself is suppressed for as long as the introduction runs,
and a transcript boundary that no card marks has to stand clear of the ones that
do.
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

# How far before a section's own start a card may sit and still be read as the
# card that opened it. The slide goes up while the previous sentence is still
# finishing, and the topic pass only locates a boundary to the nearest block.
LOOKBACK_SECONDS = settings.video_topic_window_before_seconds

# A boundary the transcript found but no slide marked has to earn its place.
# Below this, it is the same turn described twice — a "Q&A" the transcript hears
# a couple of minutes after the deck already announced "Resource Center Tour +
# Q&A" is one chapter, not two.
FALLBACK_MIN_SECONDS = settings.video_min_section_seconds

# The recurring segments keep their configured labels whatever the model called
# them, so a school's page reads the same every week. They apply only where the
# deck stayed silent: a card covering the segment names it instead, which is how
# a combined "Resource Center Tour + Q&A" slide becomes one chapter.
_FIXED_LABELS = {
    INTRODUCTION: (LABEL_INTRODUCTION, "intro"),
    TOUR: (LABEL_TOUR, "tour"),
    QNA: (LABEL_QNA, "qna"),
}

# How far past a Q&A section's own start the transcript may be searched for the
# sentence that opens it. The topic pass reads merged blocks, so its answer is
# only accurate to a block; the phrase itself is exact.
_QNA_SNAP_SECONDS = 120.0


def _opening_ends(sections: list[Section], *, lookback: float) -> float:
    """The moment a title card stops being the session's own title slide.

    The slide that names the webinar is a title card by every visible property,
    and promoting it would put a second chapter a few seconds into the
    introduction. So the introduction owns the cards inside it — with one
    exception, because the first section's card goes up while the host is still
    finishing the welcome, which puts it *before* the boundary the transcript
    found.

    The exception is bounded twice over: by the look-back, and by the halfway
    mark of the opening. A card in the first half of the introduction is the
    session's title slide however short the introduction turned out to be; a
    card at the end of it is the first section arriving early.
    """
    if not any(section.kind == INTRODUCTION for section in sections):
        return 0.0
    first_topic = next(
        (section.start for section in sections if section.kind != INTRODUCTION), None
    )
    if first_topic is None:
        return 0.0
    return max(first_topic / 2.0, first_topic - lookback)


def _cards(frames: list[Classified], *, after: float) -> list[Classified]:
    """Every title card the deck showed once the session proper had begun.

    Grouped by normalised heading and dated from the earliest copy: the sampler
    emits a frame per visual change, so a card re-read after a cut back to the
    presenter appears again, later than the section it opened.
    """
    earliest: dict[str, Classified] = {}
    for frame in frames:
        if frame.type != TITLE_CARD or not frame.heading.strip():
            continue
        if frame.timestamp < after:
            continue
        key = normalise_heading(frame.heading)
        if not key:
            continue
        held = earliest.get(key)
        if held is None or frame.timestamp < held.timestamp:
            earliest[key] = frame
    return sorted(earliest.values(), key=lambda frame: frame.timestamp)


def _is_covered(cards: list[Classified], low: float, high: float) -> bool:
    """Whether a card already opens the stretch ``[low, high)``.

    A section a card covers emits nothing of its own — the card is the chapter.
    """
    return any(low <= card.timestamp < high for card in cards)


def _qna_timecode(section: Section, cues: list[Cue]) -> float:
    """Snap a Q&A section onto the sentence that opens it, when there is one."""
    spoken = find_qna_start(cues, section.start - LOOKBACK_SECONDS)
    if spoken is None or abs(spoken - section.start) > _QNA_SNAP_SECONDS:
        return section.start
    return spoken


def _thin_fallbacks(chapters: list[Chapter], min_seconds: float) -> list[Chapter]:
    """Drop a transcript-derived chapter that opens too soon after a card.

    Cards are never dropped here — the deck showing a new slide is the evidence
    this rule exists to defer to. Only the chapters the transcript supplied on
    its own are held to the floor, and only against a card: two transcript
    boundaries were already spaced by the topic pass, which deliberately lets a
    tour run straight into the Q&A. Holding them apart again here dropped that
    Q&A from a deck with no title cards at all.
    """
    if min_seconds <= 0:
        return chapters
    kept: list[Chapter] = []
    for chapter in chapters:
        if kept and chapter.source != TITLE_CARD and kept[-1].source == TITLE_CARD:
            gap = chapter.timecode - kept[-1].timecode
            if gap < min_seconds:
                logger.info(
                    "dropping %r at %s — %ds after %r, which the deck marked",
                    chapter.title,
                    format_timestamp(float(chapter.timecode)),
                    gap,
                    kept[-1].title,
                )
                continue
        kept.append(chapter)
    return kept


def build_from_sections(
    sections: list[Section],
    frames: list[Classified],
    *,
    cues: list[Cue] | None = None,
    max_chapters: int = settings.video_max_chapters,
    lookback: float = LOOKBACK_SECONDS,
    fallback_min_seconds: float = FALLBACK_MIN_SECONDS,
) -> list[Chapter]:
    """A chapter per title card, plus the sections the deck left unnamed."""
    ordered = sorted(sections, key=lambda section: section.start)

    cards = _cards(frames, after=_opening_ends(ordered, lookback=lookback))

    chapters = [
        Chapter(int(card.timestamp), card.heading.strip(), TITLE_CARD) for card in cards
    ]

    for index, section in enumerate(ordered):
        # The stretch of the recording this section would answer for. Both edges
        # carry the look-back, so a card that went up shortly before a boundary
        # belongs to the section it opens rather than the one it interrupts —
        # without that on the upper edge, the introduction claims the first
        # section's card and then names itself after nothing.
        low = max(0.0, section.start - lookback)
        if index:
            low = max(low, ordered[index - 1].start)
        high = (
            ordered[index + 1].start - lookback
            if index + 1 < len(ordered)
            else float("inf")
        )
        if _is_covered(cards, low, high):
            continue

        fixed = _FIXED_LABELS.get(section.kind)
        if fixed is not None:
            label, source = fixed
            start = _qna_timecode(section, cues or []) if section.kind == QNA else section.start
            chapters.append(Chapter(int(start), label, source))
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
            "no title card in the section at %s — using the transcript label %r",
            format_timestamp(section.start),
            section.label,
        )
        chapters.append(Chapter(int(section.start), section.label, "topic"))

    # Cards sort ahead of a fallback landing on the same second, so the one the
    # thinning below keeps is the one the deck put there.
    chapters.sort(key=lambda chapter: (chapter.timecode, chapter.source != TITLE_CARD))
    chapters = _thin_fallbacks(chapters, fallback_min_seconds)
    chapters = dedupe(chapters)

    # Vimeo expects the timeline to start at zero, and the first section is the
    # opening one whatever the transcript said about where speech began.
    if chapters and chapters[0].timecode != 0:
        first = chapters[0]
        chapters[0] = Chapter(0, first.title, first.source)

    capped, _ = apply_cap(chapters, max_chapters)
    return capped


__all__ = ["build_from_sections"]
