"""Cut one ~60 second trailer reel from a job's transcript and recordings.

The stages, in order, each reported through `on_stage` for the admin screen:

  selecting    Bedrock picks sentence runs, steered by the admin's prompt
  cutting      snap each run's ends into pauses, join the camera audio
  transcribing AWS Transcribe word timings of the joined cut
  captioning   TikTok-style ASS captions, hook title, lower third, end card
  rendering    camera/screen edit + burned captions + loudnorm

``scripts/debug/trailer_reel_local.py`` runs the same stages with a cache per
stage, for tuning against production without writing anything back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from src.config import settings
from src.video_pipeline.ffmpeg_ops import probe_duration
from src.video_pipeline.reel_sources import ReelInputs
from src.video_pipeline.trailer_captions import CTA_SECONDS, LAYOUTS, build_ass
from src.video_pipeline.trailer_edit import SHRINK_SECONDS, plan_shots, screen_windows
from src.video_pipeline.trailer_render import cut_segments, extract_audio, render_reel
from src.video_pipeline.trailer_select import Segment, select_segments
from src.video_pipeline.trailer_sentences import snap_to_pause, speakers, split_sentences
from src.video_pipeline.trailer_words import transcribe_words
from src.video_pipeline.transcript import Cue

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).parent / "fonts"
CTA_TEXT = "Watch the full webinar"


@dataclass(frozen=True)
class BuiltReel:
    path: Path
    hook_title: str
    duration_seconds: float


def build_reel(
    inputs: ReelInputs,
    *,
    camera: Path,
    screen: Path,
    orientation: str,
    focus: str | None,
    work_dir: Path,
    scratch_prefix: str,
    on_stage: Callable[[str], None] = lambda _stage: None,
) -> BuiltReel:
    """Render the reel into `work_dir`. `scratch_prefix` is the S3 prefix the
    reel audio passes through on its way to Transcribe (deleted after)."""
    on_stage("selecting")
    sentences = split_sentences(inputs.cues)
    selection = select_segments(sentences, inputs.chapters, inputs.title, focus=focus)

    on_stage("cutting")
    offset = inputs.trim_offset
    cuts = [snapped(camera, segment, offset) for segment in selection.segments]
    cut = cut_segments(camera, cuts, work_dir / "reel-cut.mov", offset=offset)

    on_stage("transcribing")
    words = transcribe_words(extract_audio(cut, work_dir / "reel-audio.flac"), scratch_prefix)

    on_stage("captioning")
    duration = probe_duration(cut)
    # The end card plays over the camera, clear of the last transition.
    windows = screen_windows([c.duration for c in cuts], hold_end=CTA_SECONDS + SHRINK_SECONDS)
    subtitles = work_dir / "reel.ass"
    subtitles.write_text(
        build_ass(words, duration=duration, layout=LAYOUTS[orientation],
                  title=selection.hook_title, cta=CTA_TEXT,
                  presenter=presenter_label(inputs.cues), screen=windows),
        encoding="utf-8",
    )

    on_stage("rendering")
    dest = render_reel(cut, subtitles, work_dir / "reel.mp4", orientation=orientation,
                       fonts_dir=FONTS_DIR, camera=camera, screen=screen,
                       shots=plan_shots(cuts, windows, offset=offset))
    return BuiltReel(path=dest, hook_title=selection.hook_title,
                     duration_seconds=round(probe_duration(dest), 2))


def snapped(source: Path, segment: Segment, offset: float) -> Segment:
    """Move both ends of a segment into the nearest pause in the speech.

    Sentence times are estimates, so an unsnapped cut can clip a word. Inside
    a long Zoom cue the estimate runs up to ~1.5 s early or late, hence the 2 s
    reach on the side the sentence continues into.
    """
    start = snap_to_pause(source, segment.start + offset, before=2.0, after=0.4) - offset
    end = snap_to_pause(source, segment.end + offset, before=0.4, after=2.0) - offset
    logger.info("Cut %.2f-%.2f → %.2f-%.2f", segment.start, segment.end, start, end)
    return replace(segment, start=round(start, 3), end=round(end, 3))


def presenter_label(cues: list[Cue]) -> str:
    """Zoom's label for the presenter, "Name, Company" → "Name · Company"."""
    name = settings.trailer_presenter_name.casefold()
    for label in speakers(cues):
        if label.casefold().startswith(name):
            return " · ".join(part.strip() for part in label.split(",", 1))
    return settings.trailer_presenter_name
