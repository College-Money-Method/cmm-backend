"""Reading a webinar's real sections out of its transcript.

The deck cannot answer this on its own. A heading-only slide opens a section
some weeks and lands mid-explanation others, and the two look identical, so the
frames alone produced eleven chapters where a viewer wanted seven. These tests
cover the guards rather than the model's judgement: every one of them fails
towards an empty list, because the caller reads that as "chapter from the frames
as before" while a wrong section list deletes chapters the frames got right.
"""

from __future__ import annotations

import pytest

from src.video_pipeline import topic_segment
from src.video_pipeline.bedrock_client import BedrockCallError
from src.video_pipeline.topic_segment import (
    CONTENT,
    INTRODUCTION,
    QNA,
    TOUR,
    build_prompt,
    detect_sections,
    merge_blocks,
)
from src.video_pipeline.transcript import Cue


def cues(count: int = 200, seconds: float = 10.0) -> list[Cue]:
    """A transcript of evenly spaced cues, `seconds` apart."""
    return [
        Cue(start=i * seconds, end=(i + 1) * seconds, text=f"sentence {i}")
        for i in range(count)
    ]


@pytest.fixture
def reply(monkeypatch):
    """Stand in for the Bedrock call; tests set what the model answered."""
    answer: dict = {"sections": []}

    def call_json(*, system, content, max_tokens, invoke_type):
        return answer, 100, 20

    monkeypatch.setattr(topic_segment, "call_json", call_json)
    return answer


def _section(block: int, kind: str = CONTENT, label: str = "A topic") -> dict:
    return {"block": block, "kind": kind, "label": label}


# ── blocks ───────────────────────────────────────────────────────────────────


def test_cues_are_merged_into_blocks_of_about_the_configured_length():
    """Zoom emits a cue per sentence, so a per-cue prompt buries the structure
    the question is about."""
    blocks = merge_blocks(cues(20, seconds=10.0), block_seconds=20.0)

    assert [b.start for b in blocks] == [0.0, 20.0, 40.0, 60.0, 80.0, 100.0,
                                         120.0, 140.0, 160.0, 180.0]
    assert blocks[0].text == "sentence 0 sentence 1"


def test_a_trailing_part_block_is_not_dropped():
    """The last minutes of a session are the Q&A, which is a chapter."""
    blocks = merge_blocks(cues(5, seconds=10.0), block_seconds=20.0)

    assert [b.start for b in blocks] == [0.0, 20.0, 40.0]


def test_the_prompt_numbers_every_block_with_its_timestamp():
    """The numbers are what the reply names, so a boundary lands on a moment the
    transcript actually contains."""
    rendered = build_prompt(merge_blocks(cues(4, seconds=10.0), block_seconds=20.0))

    assert rendered.splitlines() == [
        "[0] 0:00 sentence 0 sentence 1",
        "[1] 0:20 sentence 2 sentence 3",
    ]


# ── the reply is taken apart ─────────────────────────────────────────────────


def test_a_section_takes_the_start_of_the_block_it_names(reply):
    reply["sections"] = [_section(0, INTRODUCTION, "Welcome"), _section(30)]

    found = detect_sections(cues())

    assert [(s.start, s.kind) for s in found] == [(0.0, INTRODUCTION), (600.0, CONTENT)]


def test_sections_come_back_in_time_order_however_they_were_listed(reply):
    reply["sections"] = [_section(50), _section(0, INTRODUCTION), _section(25)]

    assert [s.start for s in detect_sections(cues())] == [0.0, 500.0, 1000.0]


def test_a_block_number_that_does_not_exist_is_dropped(reply):
    """A composed timestamp is the failure this guard exists for: it puts a
    chapter somewhere the speaker never turned."""
    reply["sections"] = [_section(0), _section(9999)]

    assert [s.start for s in detect_sections(cues())] == [0.0]


def test_a_block_that_is_not_an_integer_is_dropped(reply):
    reply["sections"] = [_section(0), {"block": "twelve", "kind": CONTENT, "label": "x"},
                         {"block": True, "kind": CONTENT, "label": "x"}]

    assert [s.start for s in detect_sections(cues())] == [0.0]


def test_an_unknown_kind_is_read_as_ordinary_content(reply):
    """Only the four kinds map onto a label. An invented one must not silently
    become a recurring segment."""
    reply["sections"] = [_section(0, "summary")]

    assert detect_sections(cues())[0].kind == CONTENT


# ── the length floor ─────────────────────────────────────────────────────────


def test_two_content_sections_too_close_together_become_one(reply):
    """The bogus chapters all looked like this: a sub-heading fifty seconds into
    the section it belongs to."""
    reply["sections"] = [_section(0), _section(5), _section(60)]

    assert [s.start for s in detect_sections(cues(), min_seconds=240.0)] == [0.0, 1200.0]


def test_a_recurring_segment_is_exempt_from_the_floor(reply):
    """A tour running straight into the Q&A is two real chapters minutes apart.
    Dropping one to satisfy a spacing rule loses the boundary a viewer most
    wants."""
    reply["sections"] = [_section(0, TOUR, "Resource centre"), _section(8, QNA, "Questions")]

    found = detect_sections(cues(), min_seconds=240.0)

    assert [(s.start, s.kind) for s in found] == [(0.0, TOUR), (160.0, QNA)]


def test_a_second_tour_keeps_its_own_wording_instead_of_being_deduped(reply):
    """A session that demos two parts of the product comes back with two tours.
    Both would be titled "Resource center tour" and the second deduped away, so
    the repeat is demoted to content and keeps the boundary."""
    reply["sections"] = [
        _section(0, TOUR, "Resource centre"),
        _section(40, TOUR, "Counselor hub"),
    ]

    found = detect_sections(cues(), min_seconds=240.0)

    assert [(s.kind, s.label) for s in found] == [
        (TOUR, "Resource centre"),
        (CONTENT, "Counselor hub"),
    ]


# ── falling back ─────────────────────────────────────────────────────────────


def test_no_transcript_means_no_sections():
    assert detect_sections([]) == []


def test_a_failed_call_falls_back_rather_than_failing_the_job(monkeypatch):
    def boom(*, system, content, max_tokens, invoke_type):
        raise BedrockCallError("throttled")

    monkeypatch.setattr(topic_segment, "call_json", boom)

    assert detect_sections(cues()) == []


def test_a_reply_with_no_sections_key_falls_back(reply):
    reply.clear()
    reply["chapters"] = [_section(0)]

    assert detect_sections(cues()) == []


def test_a_reply_where_nothing_survives_the_guards_falls_back(reply):
    reply["sections"] = [_section(9999)]

    assert detect_sections(cues()) == []


def test_a_reply_over_the_ceiling_is_ignored_whole(reply):
    """Not a granular answer — a broken one. Trusting it would replace the
    frames' reading of the deck with noise."""
    reply["sections"] = [_section(i * 15) for i in range(6)]

    assert detect_sections(cues(), min_seconds=0.0, max_sections=4) == []
