"""The advisory cross-check between a chapter's title and the speech around it.

The single property that must hold: this never changes the chapter list. It
exists to flag a title the vision model may have invented, and a presenter who
does not read a slide aloud is common enough that acting on the flag
automatically would drop real sections.
"""

from __future__ import annotations

from src.video_pipeline.chapter_build import LABEL_INTRODUCTION, LABEL_QNA, Chapter
from src.video_pipeline.chapter_confidence import MATCH, NO_MATCH, annotate
from src.video_pipeline.frame_classify import TITLE_CARD
from src.video_pipeline.transcript import Cue


def card(timecode: int, title: str) -> Chapter:
    return Chapter(timecode=timecode, title=title, source=TITLE_CARD)


def cue(start: float, text: str) -> Cue:
    return Cue(start=start, end=start + 5.0, text=text)


def test_a_title_read_aloud_nearby_is_confirmed():
    chapters = [card(300, "The Aid Formula")]
    cues = [cue(298, "So next up is the aid formula, which is where most families get lost.")]

    assert annotate(chapters, cues, window=60)[0].confidence == MATCH


def test_a_title_nobody_says_is_flagged():
    chapters = [card(300, "Merit Scholarship Strategy")]
    cues = [cue(300, "Any questions before we move on?")]

    assert annotate(chapters, cues, window=60)[0].confidence == NO_MATCH


def test_the_flag_never_removes_or_reorders_a_chapter():
    chapters = [card(0, "Opening"), card(300, "Merit Scholarship Strategy")]

    annotated = annotate(chapters, [cue(0, "nothing relevant at all")], window=60)

    assert [(c.timecode, c.title, c.source) for c in annotated] == [
        (c.timecode, c.title, c.source) for c in chapters
    ]


def test_speech_outside_the_window_does_not_confirm():
    """A phrase said an hour earlier says nothing about this section."""
    chapters = [card(3000, "The Aid Formula")]
    cues = [cue(60, "we will get to the aid formula later")]

    assert annotate(chapters, cues, window=60)[0].confidence == NO_MATCH


def test_a_cue_straddling_the_boundary_still_counts():
    """The presenter starts the sentence before the slide changes."""
    chapters = [card(300, "The Aid Formula")]
    cues = [Cue(start=235.0, end=302.0, text="here comes the aid formula")]

    assert annotate(chapters, cues, window=60)[0].confidence == MATCH


def test_one_common_word_out_of_several_is_not_a_match():
    """'Aid' alone matches most of a financial-aid webinar."""
    chapters = [card(300, "Understanding Institutional Aid Methodology")]
    cues = [cue(300, "aid is complicated")]

    assert annotate(chapters, cues, window=60)[0].confidence == NO_MATCH


def test_stopwords_are_ignored_when_deciding():
    """Otherwise 'The Aid Formula' would half-match on 'the' alone."""
    chapters = [card(300, "The Aid Formula")]
    cues = [cue(300, "and the point is that there is more to it")]

    assert annotate(chapters, cues, window=60)[0].confidence == NO_MATCH


def test_fixed_label_segments_are_left_unchecked():
    """Their titles come from config, so there is nothing to confirm."""
    chapters = [
        Chapter(0, LABEL_INTRODUCTION, "intro"),
        Chapter(3000, LABEL_QNA, "qna_transcript"),
    ]

    assert [c.confidence for c in annotate(chapters, [cue(0, "hello")], window=60)] == ["", ""]


def test_without_a_transcript_nothing_is_flagged():
    """An empty flag means 'not checked' — a missing transcript is not suspicion."""
    chapters = [card(300, "Merit Scholarship Strategy")]

    assert annotate(chapters, [], window=60)[0].confidence == ""


def test_a_title_of_only_stopwords_is_left_unchecked():
    chapters = [card(300, "For You")]

    assert annotate(chapters, [cue(300, "for you")], window=60)[0].confidence == ""
