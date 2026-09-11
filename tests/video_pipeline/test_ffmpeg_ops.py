"""Filter composition and stderr parsing — the parts of ffmpeg_ops without ffmpeg.

Both are silent-failure surfaces. A filter chain in the wrong order produces
thousands of near-identical frames instead of ~150, and a misparsed showinfo
line moves every chapter without changing a single frame.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.video_pipeline import ffmpeg_ops
from src.video_pipeline.ffmpeg_ops import (
    TRIM_LEAD_IN_SECONDS,
    build_sampling_filter,
    detect_speech_start,
    sample_distinct_frames,
)


def _filter(**overrides) -> str:
    r"""The chain ffmpeg is handed, as one string.

    Deliberately not split on commas: the threshold's own comma is escaped, so
    splitting would tear ``select=gt(scene\,0.05)`` in half and any ordering
    assertion built on the pieces would be asserting about a filter that does
    not exist.
    """
    kwargs = dict(fps=2.0, width=960, crop_w=0.83, crop_h=0.88, scene_threshold=0.05)
    kwargs.update(overrides)
    return build_sampling_filter(**kwargs)


class TestSamplingFilter:
    def test_crop_precedes_the_scene_filter(self):
        """A per-second clock or rolling caption scores on every frame, so an
        overlay left in forces the threshold up until it stops seeing slides."""
        chain = _filter()

        assert chain.index("crop=iw*0.83:ih*0.88:0:0") < chain.index("select=gt(scene")

    def test_scale_precedes_crop_so_fractions_are_of_the_scaled_frame(self):
        chain = _filter()

        assert chain.index("scale=960:-1") < chain.index("crop=iw*0.83:ih*0.88:0:0")

    def test_showinfo_is_last_so_it_reports_the_frames_actually_written(self):
        chain = _filter()

        assert chain.endswith(",showinfo")
        assert chain.startswith("fps=2.0,")

    def test_the_threshold_comma_is_escaped_for_the_filtergraph_parser(self):
        """ffmpeg splits the chain on commas before it reads any filter, so an
        unescaped one turns the threshold into a filter name and the whole run
        dies on an argument that looks correct."""
        assert r"select=gt(scene\,0.05)" in build_sampling_filter(
            fps=2.0, width=960, crop_w=0.83, crop_h=0.88, scene_threshold=0.05
        )

    def test_the_threshold_is_the_configured_one(self):
        """It is the single number standing between ~150 frames and thousands."""
        assert r"select=gt(scene\,0.2)" in build_sampling_filter(
            fps=2.0, width=960, crop_w=0.83, crop_h=0.88, scene_threshold=0.2
        )

    @pytest.mark.parametrize("crop_w,crop_h", [(None, None), (0.83, None), (None, 0.88)])
    def test_crop_is_omitted_unless_both_fractions_are_set(self, crop_w, crop_h):
        """A one-sided crop would silently cut a dimension nobody configured."""
        assert "crop=" not in build_sampling_filter(
            fps=2.0, width=960, crop_w=crop_w, crop_h=crop_h, scene_threshold=0.05
        )


class TestFrameTimestampPairing:
    """Frames come back as files; their times come back as stderr lines."""

    def _write_frames(self, frames_dir: Path, count: int) -> None:
        frames_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, count + 1):
            (frames_dir / f"frame_{i:04d}.jpg").write_bytes(b"jpeg")

    def _run_writing(self, monkeypatch, frames_dir: Path, stderr: str, count: int):
        """Stub ffmpeg: emit `stderr` and leave `count` frame files behind.

        Frames are written from inside the stub because sample_distinct_frames
        clears stale files first — anything staged beforehand is deleted.
        """

        def fake_run(cmd):
            self._write_frames(frames_dir, count)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=stderr)

        monkeypatch.setattr(ffmpeg_ops, "_run", fake_run)

    def test_nth_showinfo_line_pairs_with_the_nth_file(self, tmp_path, monkeypatch):
        frames = tmp_path / "frames"
        stderr = (
            "[Parsed_showinfo_3 @ 0x1] n:0 pts:0 pts_time:0.5 pos:1\n"
            "[Parsed_showinfo_3 @ 0x1] n:1 pts:2 pts_time:64 pos:2\n"
            "[Parsed_showinfo_3 @ 0x1] n:2 pts:4 pts_time:187.25 pos:3\n"
        )
        self._run_writing(monkeypatch, frames, stderr, 3)

        candidates = sample_distinct_frames(tmp_path / "in.mp4", frames)

        assert [c.timestamp for c in candidates] == [0.5, 64.0, 187.25]
        assert [c.index for c in candidates] == [1, 2, 3]
        assert [c.path.name for c in candidates] == [
            "frame_0001.jpg",
            "frame_0002.jpg",
            "frame_0003.jpg",
        ]

    def test_a_count_mismatch_truncates_rather_than_misaligning(self, tmp_path, monkeypatch):
        """Fewer pairs is recoverable; shifted pairs would move every chapter."""
        frames = tmp_path / "frames"
        self._run_writing(monkeypatch, frames, "pts_time:1.0\npts_time:2.0\n", 5)

        candidates = sample_distinct_frames(tmp_path / "in.mp4", frames)

        assert [c.timestamp for c in candidates] == [1.0, 2.0]

    def test_stale_frames_from_a_previous_run_are_cleared(self, tmp_path, monkeypatch):
        frames = tmp_path / "frames"
        frames.mkdir()
        (frames / "frame_0009.jpg").write_bytes(b"stale")

        def fake_run(cmd):
            # Stale files must already be gone by the time ffmpeg writes.
            assert not list(frames.glob("frame_*.jpg"))
            self._write_frames(frames, 1)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="pts_time:3.0\n")

        monkeypatch.setattr(ffmpeg_ops, "_run", fake_run)

        candidates = sample_distinct_frames(tmp_path / "in.mp4", frames)

        assert [c.path.name for c in candidates] == ["frame_0001.jpg"]


class TestSilenceFallback:
    """Used only when Zoom produced no transcript. Erring towards no trim."""

    def _fake_run(self, monkeypatch, stderr: str):
        monkeypatch.setattr(
            ffmpeg_ops,
            "_run",
            lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=stderr),
        )

    def test_leading_silence_becomes_a_trim_point_minus_the_lead_in(self, tmp_path, monkeypatch):
        self._fake_run(
            monkeypatch,
            "[silencedetect @ 0x1] silence_start: 0\n"
            "[silencedetect @ 0x1] silence_end: 42.5 | silence_duration: 42.5\n",
        )

        assert detect_speech_start(tmp_path / "in.mp4") == pytest.approx(
            42.5 - TRIM_LEAD_IN_SECONDS
        )

    def test_silence_that_starts_mid_recording_is_just_a_pause(self, tmp_path, monkeypatch):
        """The presenter talking from 0:00 and pausing at 5:00 is not a dead opening."""
        self._fake_run(
            monkeypatch,
            "silence_start: 300.0\nsilence_end: 310.0 | silence_duration: 10.0\n",
        )

        assert detect_speech_start(tmp_path / "in.mp4") == 0.0

    def test_no_silence_at_all_means_no_trim(self, tmp_path, monkeypatch):
        self._fake_run(monkeypatch, "no silence here\n")

        assert detect_speech_start(tmp_path / "in.mp4") == 0.0

    def test_an_answer_beyond_the_bound_is_rejected(self, tmp_path, monkeypatch):
        """A late cut destroys content; publishing untrimmed only looks sloppy."""
        self._fake_run(monkeypatch, "silence_start: 0\nsilence_end: 1200.0\n")

        assert detect_speech_start(tmp_path / "in.mp4", max_offset=900.0) == 0.0

    def test_ffmpeg_failure_degrades_to_no_trim(self, tmp_path, monkeypatch):
        def explode(cmd):
            raise ffmpeg_ops.FfmpegError("exited 1")

        monkeypatch.setattr(ffmpeg_ops, "_run", explode)

        assert detect_speech_start(tmp_path / "in.mp4") == 0.0

    def test_speech_starting_inside_the_lead_in_clamps_to_zero(self, tmp_path, monkeypatch):
        self._fake_run(monkeypatch, "silence_start: 0\nsilence_end: 0.4\n")

        assert detect_speech_start(tmp_path / "in.mp4") == 0.0
