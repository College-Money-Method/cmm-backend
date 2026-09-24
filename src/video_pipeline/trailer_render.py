"""ffmpeg passes that turn chosen segments into a finished reel.

Two encodes, on purpose:

1. ``cut_segments`` joins the segments into one 16:9 clip. Each segment is its
   own input with an input-side ``-ss``, so seeking is fast *and* frame-accurate
   (the result is re-encoded anyway), and none of the other 70-odd minutes are
   decoded. A 50 ms audio fade either side of every cut hides the click a hard
   audio cut makes. Kept at near-lossless quality: it is an intermediate.
2. ``render_reel`` takes that clip's sound, rebuilds the picture shot by shot
   from the untrimmed recordings (camera, shared screen, and the transitions
   between them: ``trailer_edit``), frames it for its orientation, burns the
   ASS captions in through libass, and normalises loudness to -14 LUFS.
   Landscape keeps the camera frame as it is; portrait crops 9:16 around the
   presenter and scales up, which costs sharpness (a 720p source leaves a
   405 px wide crop). A portrait screen shot stacks the slide, a caption band
   and the presenter.

Keeping them apart lets the captions, which need the joined clip's audio to be
transcribed first, be rebuilt and re-burned without cutting again.
"""

from __future__ import annotations

from pathlib import Path

from src.video_pipeline.ffmpeg_ops import FfmpegError, _run
from src.video_pipeline.trailer_edit import (
    CAMERA,
    SCREEN,
    ZOOM_THUMBNAIL,
    Shot,
    join_graph,
    read_span,
)
from src.video_pipeline.trailer_select import Segment

PORTRAIT_SIZE = (1080, 1920)
PORTRAIT_SLIDE_TOP = 160  # clear of the app chrome at the top of a phone screen
PORTRAIT_SLIDE_HEIGHT = round(PORTRAIT_SIZE[0] * 9 / 16)
# A cmm-teal band between slide and presenter on a portrait screen shot: the
# captions go here, where they cover neither the slide nor the presenter's face.
# As tall as the caption bar (Inter ExtraBold 85 plus padding renders 119 px),
# so the bar merges into it and the words read as text on the band.
PORTRAIT_CAPTION_BAND = 120
PORTRAIT_PRESENTER_TOP = PORTRAIT_SLIDE_TOP + PORTRAIT_SLIDE_HEIGHT + PORTRAIT_CAPTION_BAND
PORTRAIT_BACKGROUND = "0x111E24"  # brand-950
BRAND_TEAL = "0x4F788D"  # cmm-teal
FPS = 30
_FADE_SECONDS = 0.05


def cut_segments(source: Path, segments: list[Segment], dest: Path, *, offset: float = 0.0) -> Path:
    """Join `segments` of `source` into one clip at `dest` (a .mov: PCM audio).

    Segment times are on the transcript's clock; `offset` is added to reach the
    source's clock (the trim offset, when cutting the untrimmed recording).
    """
    if not segments:
        raise FfmpegError("no segments to cut")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    chains, pads = [], []
    for i, segment in enumerate(segments):
        length = segment.duration
        cmd += ["-ss", f"{segment.start + offset:.3f}", "-t", f"{length:.3f}", "-i", str(source)]
        fade_out = max(0.0, length - _FADE_SECONDS)
        chains.append(
            f"[{i}:v]fps={FPS},setpts=PTS-STARTPTS[v{i}];"
            f"[{i}:a]asetpts=PTS-STARTPTS,afade=t=in:d={_FADE_SECONDS},"
            f"afade=t=out:st={fade_out:.3f}:d={_FADE_SECONDS}[a{i}]"
        )
        pads.append(f"[v{i}][a{i}]")
    graph = ";".join(chains) + ";" + "".join(pads) + f"concat=n={len(segments)}:v=1:a=1[v][a]"
    cmd += [
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", str(dest),
    ]
    _run(cmd)
    return dest


def extract_audio(clip: Path, dest: Path) -> Path:
    """Mono 16 kHz FLAC: all a speech recogniser needs, a fraction of the size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-vn",
          "-ac", "1", "-ar", "16000", "-c:a", "flac", str(dest)])
    return dest


def render_reel(
    audio: Path,
    subtitles: Path,
    dest: Path,
    *,
    orientation: str,
    fonts_dir: Path,
    camera: Path,
    shots: list[Shot],
    screen: Path | None = None,
    crop_x: float = 0.5,
) -> Path:
    """Build the picture from `shots`, burn captions, normalise loudness, encode.

    The sound is `audio`'s (the joined cut the captions were timed against). The
    picture is rebuilt shot by shot from the untrimmed `camera` and `screen`
    recordings, joined as ``trailer_edit`` describes.

    Landscape is rendered at the camera's own size: upscaling adds no detail.
    Portrait crops 9:16; `crop_x` places the crop window across the frame (0 is
    the left edge, 1 the right, 0.5 centred). Zoom's speaker view puts the face
    in the middle, and the centred crop also drops Zoom's name tag and clock in
    the bottom corners. A portrait screen shot stacks the slide, a teal caption
    band and the presenter (cropped wider) on a brand-950 ground.
    """
    if orientation not in ("portrait", "landscape"):
        raise FfmpegError(f"unknown orientation {orientation!r}")
    if not 0.0 <= crop_x <= 1.0:
        raise FfmpegError(f"crop_x must be within 0-1, got {crop_x}")
    if screen is None and any(shot.source == SCREEN for shot in shots):
        raise FfmpegError("screen shots need the screen recording")
    portrait = orientation == "portrait"
    width, height = PORTRAIT_SIZE if portrait else probe_size(camera)
    cam_w, cam_h = probe_size(camera)
    # Portrait: the 9:16 crop, and the wider crop that fills the space under the slide.
    slide_h, low_top = PORTRAIT_SLIDE_HEIGHT, PORTRAIT_PRESENTER_TOP
    tight_w, wide_w = cam_h * 9 / 16, cam_h * width / (height - low_top)
    if portrait:
        # The 9:16 crop's own spot inside the wider crop: the camera shrinks into it.
        low_scale = (height - low_top) / cam_h
        camera_rect = (((cam_w - tight_w) - (cam_w - wide_w)) * crop_x * low_scale, low_top,
                       tight_w * low_scale, height - low_top)
    else:
        camera_rect = tuple(f * d for f, d in zip(ZOOM_THUMBNAIL, (width, height) * 2))
    tight = f"crop={tight_w:.0f}:ih:(iw-ow)*{crop_x}:0,scale={width}:{height}:flags=lanczos,"
    wide = (f"crop={wide_w:.0f}:ih:(iw-ow)*{crop_x}:0,"
            f"scale={width}:{height - low_top}:flags=lanczos,"
            f"pad={width}:{height}:0:{low_top}:color={PORTRAIT_BACKGROUND}")
    # Each shot's stream starts at 0 and runs at the reel's rate; xfade needs
    # yuv444p for the shrink's sampling, and a constant frame rate.
    lead, norm = f"setpts=PTS-STARTPTS,fps={FPS}", "setsar=1,format=yuv444p"
    inputs, chains, pieces = ["-i", str(audio)], [], []

    def read(path: Path, k: int) -> int:
        start, length = read_span(shots, k, FPS)
        inputs.extend(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path)])
        return inputs.count("-i") - 1

    for k, shot in enumerate(shots):
        label = f"p{k}"
        if shot.source == CAMERA:
            frame = tight if portrait else ""
            chains.append(f"[{read(camera, k)}:v]{lead},{frame}{norm}[{label}]")
        elif portrait:
            cam, scr = read(camera, k), read(screen, k)
            chains += [f"[{cam}:v]{lead},{wide}[low{k}]",
                       f"[{scr}:v]{lead},scale={width}:{slide_h}:flags=lanczos[slide{k}]",
                       f"[low{k}][slide{k}]overlay=0:{PORTRAIT_SLIDE_TOP},"
                       f"drawbox=x=0:y={PORTRAIT_SLIDE_TOP + slide_h}:w={width}:"
                       f"h={PORTRAIT_CAPTION_BAND}:color={BRAND_TEAL}:t=fill,{norm}[{label}]"]
        else:
            chains.append(f"[{read(screen, k)}:v]{lead},"
                          f"scale={width}:{height}:flags=lanczos,{norm}[{label}]")
        pieces.append(label)
    chains.append(join_graph(shots, pieces, "edit", size=(width, height),
                             camera_rect=camera_rect))
    chains.append(f"[edit]subtitles=filename={_filter_path(subtitles)}:"
                  f"fontsdir={_filter_path(fonts_dir)}[v]")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "0:a",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", str(dest),
    ])
    return dest


def probe_size(path: Path) -> tuple[int, int]:
    """Width and height of the first video stream."""
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
    width, height = proc.stdout.strip().split(",")[:2]
    return int(width), int(height)


def _filter_path(path: Path) -> str:
    """Quote a path for use as a filtergraph option value."""
    text = str(path.resolve()).replace("\\", "/")
    return "'" + text.replace("'", r"'\''").replace(":", r"\:") + "'"
