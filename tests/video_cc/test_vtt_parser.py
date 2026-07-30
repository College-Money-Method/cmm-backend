"""Unit tests for WebVTT parsing, chunking and re-serialisation.

The invariant that matters: cue timings, identifiers and settings must survive a
parse → translate → serialize round trip byte-for-byte, because a shifted
timestamp silently desynchronises the whole caption track.
"""

import pytest

from src.content.vtt_parser import VttError, chunk_cues, parse, serialize

SAMPLE = """WEBVTT

NOTE Recorded 2026-01-14

intro
00:00:00.000 --> 00:00:02.500 align:start position:10%
Welcome to College Money Method.

00:00:02.500 --> 00:00:06.000
This cue spans
two lines.

00:00:06.000 --> 00:00:08.000

3
00:00:08.000 --> 00:00:11.000
Final cue.
"""


def test_parses_only_cues_with_text():
    doc = parse(SAMPLE)
    # The timed-but-empty cue is passthrough, not a translatable cue.
    assert len(doc.cues) == 3
    assert [c.text for c in doc.cues] == [
        "Welcome to College Money Method.",
        "This cue spans\ntwo lines.",
        "Final cue.",
    ]


def test_preserves_identifier_and_cue_settings():
    doc = parse(SAMPLE)
    assert doc.cues[0].identifier == "intro"
    assert doc.cues[0].timing == "00:00:00.000 --> 00:00:02.500 align:start position:10%"
    assert doc.cues[2].identifier == "3"


def test_round_trip_preserves_timings_and_metadata():
    doc = parse(SAMPLE)
    out = serialize(doc, {0: "Bienvenido.", 1: "Dos lineas.", 2: "Fin."})

    assert out.startswith("WEBVTT")
    assert "NOTE Recorded 2026-01-14" in out
    for timing in (
        "00:00:00.000 --> 00:00:02.500 align:start position:10%",
        "00:00:02.500 --> 00:00:06.000",
        "00:00:06.000 --> 00:00:08.000",
        "00:00:08.000 --> 00:00:11.000",
    ):
        assert timing in out
    assert "Bienvenido." in out and "Fin." in out


def test_untranslated_cues_fall_back_to_source_text():
    """A model that drops a key must not produce an empty caption."""
    doc = parse(SAMPLE)
    out = serialize(doc, {0: "Bienvenido."})
    assert "Bienvenido." in out
    assert "This cue spans\ntwo lines." in out
    assert "Final cue." in out


def test_srt_input_is_converted_to_webvtt():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nHello\n\n2\n00:00:03,000 --> 00:00:05,500\nWorld\n"
    doc = parse(srt)
    assert len(doc.cues) == 2
    out = serialize(doc, {})
    assert out.startswith("WEBVTT")
    # Comma millisecond separators become periods; times themselves unchanged.
    assert "00:00:01.000 --> 00:00:03.000" in out
    assert "," not in out.split("\n")[2]


def test_bom_prefixed_file_still_parses():
    doc = parse("﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
    assert len(doc.cues) == 1


def test_hourless_timestamps_are_accepted():
    doc = parse("WEBVTT\n\n01:02.500 --> 01:05.000\nShort form\n")
    assert len(doc.cues) == 1
    assert doc.cues[0].timing == "01:02.500 --> 01:05.000"


@pytest.mark.parametrize("bad", ["", "   ", "just prose, no timings at all"])
def test_files_without_cues_are_rejected(bad):
    with pytest.raises(VttError):
        parse(bad)


def test_chunking_respects_count_and_char_budget():
    doc = parse(SAMPLE)
    assert [len(b) for b in chunk_cues(doc.cues, char_budget=10_000, max_count=2)] == [2, 1]
    # A tiny budget isolates each cue rather than dropping any.
    single = chunk_cues(doc.cues, char_budget=1, max_count=25)
    assert [len(b) for b in single] == [1, 1, 1]


def test_chunking_never_loses_a_cue():
    doc = parse(SAMPLE)
    batched = [c.index for batch in chunk_cues(doc.cues, 40, 2) for c in batch]
    assert batched == [c.index for c in doc.cues]


def test_blank_translation_falls_back_instead_of_emitting_an_empty_cue():
    """Observed live: the model returned "" for one cue, publishing a silent gap."""
    doc = parse(SAMPLE)
    out = serialize(doc, {0: "", 1: "   ", 2: "Fin."})

    # Every cue keeps text — a timing line must never be followed by nothing.
    assert "Welcome to College Money Method." in out
    assert "This cue spans\ntwo lines." in out
    assert "Fin." in out
    # Round-tripping the result must recover the same number of cues.
    assert len(parse(out).cues) == len(doc.cues)
