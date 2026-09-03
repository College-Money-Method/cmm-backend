"""Unit tests for the transcript adapter over the shared VTT parser.

Parsing itself is covered by tests/video_cc/test_vtt_parser.py. What matters
here is the pipeline-specific part: re-basing cues onto the trimmed video's
clock. Every chapter timecode is measured against that clock, so an off-by-one
in `rebase` shifts the whole chapter list.
"""

from pathlib import Path

import pytest

from src.video_pipeline.transcript import (
    Cue,
    VttError,
    format_timestamp,
    load_cues,
    rebase,
)

SAMPLE = """WEBVTT

1
00:00:01.500 --> 00:00:06.000
We're just waiting for families to come in.

2
00:00:47.000 --> 00:00:55.000
Alrighty, it looks like we have attendance.

3
00:01:02.250 --> 00:01:09.000
Tonight we're talking about paying for college.
"""


class TestLoadCues:
    def test_reads_start_end_and_text(self, tmp_path: Path):
        path = tmp_path / "sample.vtt"
        path.write_text(SAMPLE)
        cues = load_cues(path)
        assert len(cues) == 3
        assert cues[0].start == pytest.approx(1.5)
        assert cues[0].end == pytest.approx(6.0)
        assert cues[2].start == pytest.approx(62.25)
        assert "paying for college" in cues[2].text

    def test_multi_line_cue_text_is_joined(self, tmp_path: Path):
        path = tmp_path / "multi.vtt"
        path.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nfirst line\nsecond line\n"
        )
        cues = load_cues(path)
        assert len(cues) == 1
        assert "first line" in cues[0].text and "second line" in cues[0].text

    def test_garbage_raises(self, tmp_path: Path):
        path = tmp_path / "bad.vtt"
        path.write_text("this is not a caption file at all")
        with pytest.raises(VttError):
            load_cues(path)


class TestRebase:
    def test_shifts_cues_back_by_the_offset(self):
        cues = [Cue(start=50.0, end=56.0, text="a"), Cue(start=60.0, end=66.0, text="b")]
        out = rebase(cues, 46.0)
        assert [(c.start, c.end) for c in out] == [(4.0, 10.0), (14.0, 20.0)]

    def test_drops_cues_that_end_before_the_cut(self):
        cues = [
            Cue(start=1.0, end=6.0, text="stalling"),
            Cue(start=50.0, end=56.0, text="content"),
        ]
        out = rebase(cues, 46.0)
        assert [c.text for c in out] == ["content"]

    def test_cue_straddling_the_cut_is_clamped_to_zero(self):
        """The presenter is mid-sentence at the cut; the cue survives but cannot
        carry a negative start onto the trimmed clock."""
        out = rebase([Cue(start=44.0, end=50.0, text="mid-sentence")], 46.0)
        assert len(out) == 1
        assert out[0].start == 0.0
        assert out[0].end == pytest.approx(4.0)

    def test_zero_offset_is_a_no_op(self):
        cues = [Cue(start=10.0, end=12.0, text="a")]
        assert rebase(cues, 0.0) == cues

    def test_empty_input(self):
        assert rebase([], 46.0) == []


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "0:00"),
            (5.0, "0:05"),
            (65.0, "1:05"),
            (600.0, "10:00"),
            (3600.0, "1:00:00"),
            (3661.0, "1:01:01"),
            (5425.0, "1:30:25"),
        ],
    )
    def test_formats(self, seconds: float, expected: str):
        assert format_timestamp(seconds) == expected

    def test_fractional_seconds_truncate(self):
        assert format_timestamp(65.9) == "1:05"
