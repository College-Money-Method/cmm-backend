"""Turning transcript sections into chapters, titled by the slides near them.

The transcript says where a section starts; it cannot say what the deck calls
it, and its own idea of "start" is only accurate to a merged block. So each
boundary is given a window, and the frames inside it supply the wording and the
exact timecode. Everything here is about that window: what it may promote, what
it must ignore, and what happens when it holds nothing at all.
"""

from __future__ import annotations

from src.video_pipeline.chapter_build import LABEL_INTRODUCTION, LABEL_QNA, LABEL_TOUR
from src.video_pipeline.frame_classify import CONTENT_SLIDE, SPEAKER, TITLE_CARD, Classified
from src.video_pipeline.section_chapters import build_from_sections, in_any_window, windows
from src.video_pipeline.topic_segment import CONTENT, INTRODUCTION, QNA, TOUR, Section
from src.video_pipeline.transcript import Cue


def frame(timestamp: float, type_: str, heading: str = "") -> Classified:
    return Classified(
        index=int(timestamp),
        timestamp=timestamp,
        file=f"frame_{int(timestamp):04d}.jpg",
        type=type_,
        heading=heading,
    )


def section(start: float, kind: str = CONTENT, label: str = "A topic") -> Section:
    return Section(start=start, kind=kind, label=label)


# ── the windows ──────────────────────────────────────────────────────────────


def test_a_window_reaches_further_back_than_forward():
    """The card goes up before the sentence that introduces it finishes, so the
    look-back is the half that has to be generous."""
    assert windows([section(1000.0)], before=90.0, after=45.0) == [(910.0, 1045.0)]


def test_a_window_never_starts_before_the_recording_does():
    assert windows([section(10.0)], before=90.0, after=45.0) == [(0.0, 55.0)]


def test_a_frame_is_read_only_inside_some_window():
    spans = [(0.0, 55.0), (910.0, 1045.0)]

    assert in_any_window(30.0, spans) is True
    assert in_any_window(1045.0, spans) is True
    assert in_any_window(400.0, spans) is False


# ── titles come from the deck ────────────────────────────────────────────────


def test_a_title_card_in_the_window_names_the_section_and_sets_its_time():
    """The slide's own wording beats the model's paraphrase, and the slide's
    timestamp beats the block the sentence fell in."""
    sections = [section(0.0, INTRODUCTION), section(1000.0, label="how aid is worked out")]
    frames = [
        frame(0.0, SPEAKER),
        frame(980.0, TITLE_CARD, "The Aid Formula"),
    ]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title, c.source) for c in chapters] == [
        (0, LABEL_INTRODUCTION, "intro"),
        (980, "The Aid Formula", TITLE_CARD),
    ]


def test_the_nearest_card_wins_when_the_window_holds_two():
    """A long look-back can reach the tail of the previous section. The card that
    opens this one is the one closest to where the speaker turned."""
    frames = [
        frame(920.0, TITLE_CARD, "The Previous Topic"),
        frame(995.0, TITLE_CARD, "The Aid Formula"),
    ]

    chapters = build_from_sections([section(1000.0)], frames)

    assert [(c.timecode, c.title) for c in chapters] == [(0, "The Aid Formula")]


def test_a_card_shown_twice_is_dated_from_its_first_appearance():
    """The sampler emits a frame per visual change, so a card re-read after a cut
    back from the presenter appears again — later than the section began."""
    frames = [
        frame(970.0, TITLE_CARD, "The Aid Formula"),
        frame(990.0, SPEAKER),
        frame(1010.0, TITLE_CARD, "the aid formula"),
    ]

    chapters = build_from_sections([section(0.0, INTRODUCTION), section(1000.0)], frames)

    assert [c.timecode for c in chapters] == [0, 970]


def test_a_slide_that_is_not_a_title_card_never_titles_a_section():
    """A body slide carries a heading too, and it is not the section's name."""
    frames = [frame(990.0, CONTENT_SLIDE, "Four ways to pay")]

    chapters = build_from_sections([section(1000.0, label="paying for it")], frames)

    assert [(c.title, c.source) for c in chapters] == [("paying for it", "topic")]


def test_a_card_outside_the_window_is_ignored():
    """That is the whole point of the narrowing: a card deep inside a section is
    a change of emphasis, not a new chapter."""
    frames = [frame(600.0, TITLE_CARD, "Investment and Value")]

    chapters = build_from_sections([section(1000.0, label="paying for it")], frames)

    assert [(c.title, c.source) for c in chapters] == [("paying for it", "topic")]


def test_a_section_with_neither_a_card_nor_a_label_is_dropped():
    """A section nobody can name is one we cannot put in a menu."""
    chapters = build_from_sections(
        [section(0.0, INTRODUCTION), section(1000.0, label="")], []
    )

    assert [c.title for c in chapters] == [LABEL_INTRODUCTION]


# ── the recurring segments ───────────────────────────────────────────────────


def test_the_recurring_segments_keep_their_configured_labels():
    """A school's page has to read the same every week, whatever the presenter
    called it on the day."""
    sections = [
        section(0.0, INTRODUCTION, "Hello and welcome"),
        section(2000.0, TOUR, "let me show you the site"),
        section(3000.0, QNA, "your questions"),
    ]

    chapters = build_from_sections(sections, [])

    assert [(c.title, c.source) for c in chapters] == [
        (LABEL_INTRODUCTION, "intro"),
        (LABEL_TOUR, "tour"),
        (LABEL_QNA, "qna"),
    ]


def test_a_title_card_does_not_rename_a_recurring_segment():
    frames = [frame(1990.0, TITLE_CARD, "Your Resource Centre")]

    chapters = build_from_sections([section(2000.0, TOUR, "the site")], frames)

    assert [c.title for c in chapters] == [LABEL_TOUR]


def test_a_qna_section_snaps_onto_the_sentence_that_opens_it():
    """The topic pass reads merged blocks, so it is only accurate to a block.
    The phrase itself is exact."""
    cues = [
        Cue(start=2980.0, end=2990.0, text="and that is the last of the slides"),
        Cue(start=3020.0, end=3030.0, text="let's jump into the questions"),
    ]

    chapters = build_from_sections(
        [section(0.0, INTRODUCTION), section(3000.0, QNA)], [], cues=cues
    )

    assert [c.timecode for c in chapters] == [0, 3020]


def test_a_qna_phrase_far_from_the_section_is_not_used():
    """An early "we'll take questions at the end" is a promise, not the Q&A."""
    cues = [Cue(start=2000.0, end=2010.0, text="we will take some questions at the end")]

    chapters = build_from_sections(
        [section(0.0, INTRODUCTION), section(3000.0, QNA)], [], cues=cues
    )

    assert [c.timecode for c in chapters] == [0, 3000]


# ── the shape of the finished list ───────────────────────────────────────────


def test_the_first_chapter_is_pulled_to_the_start_of_the_video():
    """Vimeo expects the timeline to start at zero, and a menu whose first entry
    is a minute in leaves that minute unreachable."""
    chapters = build_from_sections([section(60.0, INTRODUCTION), section(1000.0)], [])

    assert [c.timecode for c in chapters] == [0, 1000]


def test_sections_out_of_order_still_produce_an_ordered_list():
    chapters = build_from_sections(
        [section(1000.0, label="Second"), section(0.0, INTRODUCTION)], []
    )

    assert [c.timecode for c in chapters] == [0, 1000]


def test_the_cap_still_applies():
    sections = [section(i * 1000.0, label=f"Topic {i}") for i in range(5)]

    chapters = build_from_sections(sections, [], max_chapters=3)

    assert len(chapters) == 3
