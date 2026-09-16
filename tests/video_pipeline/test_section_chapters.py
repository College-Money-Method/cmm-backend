"""Turning transcript sections and the deck's title cards into chapters.

The deck is the authority on where a section starts: a card is a slide the room
watched go up. The transcript supplies what the deck never titled — the opening,
and a tour or Q&A that runs on off the last slide — and the boundary that tells
a section divider apart from the session's own title slide.

Everything here is about that division of labour: what a card may name, what it
must not, and what fills the gaps it leaves.
"""

from __future__ import annotations

from src.video_pipeline.chapter_build import LABEL_INTRODUCTION, LABEL_QNA, LABEL_TOUR
from src.video_pipeline.frame_classify import CONTENT_SLIDE, SPEAKER, TITLE_CARD, Classified
from src.video_pipeline.section_chapters import build_from_sections
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


# ── titles come from the deck ────────────────────────────────────────────────


def test_a_title_card_names_the_section_and_sets_its_time():
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


def test_a_card_the_speaker_reached_late_still_names_its_section():
    """The presenter opens a topic, talks for three minutes, then advances the
    slide. The transcript boundary and the card are far apart, and the card is
    the one the audience saw — reading only around the boundary is what left a
    real recording with a paraphrased title and a missing chapter."""
    sections = [section(0.0, INTRODUCTION), section(1000.0, label="Merit-Based Aid")]
    frames = [frame(1180.0, TITLE_CARD, "Preparing to apply for financial aid")]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title, c.source) for c in chapters] == [
        (0, LABEL_INTRODUCTION, "intro"),
        (1180, "Preparing to apply for financial aid", TITLE_CARD),
    ]


def test_a_card_is_dated_from_its_first_appearance():
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


def test_every_card_after_the_opening_becomes_a_chapter():
    """One card per section is what these decks do, and each of them is a
    boundary the transcript may or may not have noticed."""
    sections = [section(0.0, INTRODUCTION), section(100.0, label="the middle")]
    frames = [
        frame(600.0, TITLE_CARD, "Investment and Value"),
        frame(1900.0, TITLE_CARD, "Completing the applications"),
    ]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title) for c in chapters] == [
        (0, LABEL_INTRODUCTION),
        (600, "Investment and Value"),
        (1900, "Completing the applications"),
    ]


def test_a_section_with_neither_a_card_nor_a_label_is_dropped():
    """A section nobody can name is one we cannot put in a menu."""
    chapters = build_from_sections(
        [section(0.0, INTRODUCTION), section(1000.0, label="")], []
    )

    assert [c.title for c in chapters] == [LABEL_INTRODUCTION]


# ── the opening ──────────────────────────────────────────────────────────────


def test_the_sessions_own_title_slide_does_not_open_a_chapter():
    """It is a title card by every visible property. Promoting it would put a
    second chapter seven seconds into the introduction."""
    sections = [section(0.0, INTRODUCTION), section(1000.0, label="the first topic")]
    frames = [
        frame(7.0, TITLE_CARD, "Applying for Financial Aid in Senior Year"),
        frame(1050.0, TITLE_CARD, "Review: how financial aid works"),
    ]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title) for c in chapters] == [
        (0, LABEL_INTRODUCTION),
        (1050, "Review: how financial aid works"),
    ]


def test_a_card_once_the_introduction_is_over_is_a_divider_again():
    """The suppression is bounded by the opening section, not by a clock."""
    sections = [section(0.0, INTRODUCTION), section(100.0, label="the first topic")]
    frames = [frame(190.0, TITLE_CARD, "Review: how financial aid works")]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title) for c in chapters] == [
        (0, LABEL_INTRODUCTION),
        (190, "Review: how financial aid works"),
    ]


# ── the recurring segments ───────────────────────────────────────────────────


def test_the_recurring_segments_keep_their_configured_labels_when_no_card_names_them():
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


def test_a_card_over_the_tour_names_it_and_absorbs_the_qna_behind_it():
    """The deck announces "Resource Center Tour + Q&A" on one slide because the
    two run together. The transcript still hears the questions start, and that
    second boundary is the same turn described twice."""
    sections = [
        section(0.0, INTRODUCTION),
        section(3668.0, TOUR, "the resource centre"),
        section(3837.0, QNA, "questions"),
    ]
    frames = [frame(3668.0, TITLE_CARD, "Resource Center Tour + Q & A")]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title, c.source) for c in chapters] == [
        (0, LABEL_INTRODUCTION, "intro"),
        (3668, "Resource Center Tour + Q & A", TITLE_CARD),
    ]


def test_a_qna_well_clear_of_the_last_card_keeps_its_own_chapter():
    """Absorbing it is about a boundary the deck already marked, not about
    dropping the Q&A wherever a card happens to precede it."""
    sections = [
        section(0.0, INTRODUCTION),
        section(2000.0, CONTENT, "the last topic"),
        section(3000.0, QNA, "questions"),
    ]
    frames = [frame(2000.0, TITLE_CARD, "Applying for merit aid")]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title, c.source) for c in chapters] == [
        (0, LABEL_INTRODUCTION, "intro"),
        (2000, "Applying for merit aid", TITLE_CARD),
        (3000, LABEL_QNA, "qna"),
    ]


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


# ── boundaries the deck never marked ─────────────────────────────────────────


def test_a_transcript_boundary_crowding_a_card_is_dropped():
    """Two boundaries a minute apart are one boundary. The card is the half with
    evidence behind it, so the paraphrase is the half that goes."""
    sections = [
        section(0.0, INTRODUCTION),
        section(1000.0, label="Merit-Based Aid"),
        section(1060.0, label="Scholarships"),
    ]
    frames = [frame(1000.0, TITLE_CARD, "Merit aid and scholarships")]

    chapters = build_from_sections(sections, frames)

    assert [(c.timecode, c.title) for c in chapters] == [
        (0, LABEL_INTRODUCTION),
        (1000, "Merit aid and scholarships"),
    ]


def test_two_cards_close_together_both_survive():
    """The floor is for boundaries the deck never marked. A deck that changes
    slide twice in a minute has said so twice."""
    sections = [section(0.0, INTRODUCTION), section(1000.0, label="the topic")]
    frames = [
        frame(1000.0, TITLE_CARD, "First half"),
        frame(1060.0, TITLE_CARD, "Second half"),
    ]

    chapters = build_from_sections(sections, frames)

    assert [c.timecode for c in chapters] == [0, 1000, 1060]


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
