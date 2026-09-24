"""The edit of a trailer reel: which recording each stretch shows, and how shots join.

A reel of one talking head, hard-cut between segments, reads as static. Two
things move it:

* **The shared screen.** Zoom's shared-screen rendition (the file the pipeline
  publishes and archives) shows the slide the presenter is talking about, with
  the presenter's camera as a thumbnail in its top-right corner. The first
  segment stays on the presenter (the hook and title card want a face). Every
  later segment opens on the presenter, moves to the screen ``SCREEN_LEAD``
  seconds in and comes back ``SCREEN_TAIL`` seconds before its end. The end
  card plays over the camera.
* **A transition at every join.** Segment to segment is a dissolve. Camera to
  screen shrinks the camera until it *is* the screen's thumbnail (portrait: the
  presenter under the slide); screen to camera grows it back to full frame.

A transition blends two shots, so each shot needs footage from past its own
edges, which the joined cut does not have. Each shot is therefore read straight
from the untrimmed recording, with half a transition extra either side. Every
Zoom file of a recording shares one clock, so camera and screen line up with no
syncing. Each join is an ``xfade`` placed at `edge - half`: the picture keeps the
joined cut's timeline exactly, so the captions and the audio (always the joined
cut's) still line up.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.video_pipeline.ffmpeg_ops import FfmpegError
from src.video_pipeline.trailer_select import Segment

CAMERA, SCREEN = "camera", "screen"
SCREEN_LEAD = 2.0
SCREEN_TAIL = 2.5
MIN_SCREEN_SECONDS = 4.0  # a shorter visit to the screen reads as a glitch
DISSOLVE_SECONDS = 0.5
SHRINK_SECONDS = 0.8
# Where Zoom's shared-screen layout puts the camera, as fractions of its frame
# (x, y, width, height): the top-right sixth. Measured on a 1920×1080 recording,
# where the 320×180 corner matches the camera frame at 40 dB PSNR.
ZOOM_THUMBNAIL = (5 / 6, 0.0, 1 / 6, 1 / 6)

Rect = tuple[float, float, float, float]  # x, y, width, height in output pixels


@dataclass(frozen=True)
class Shot:
    """One stretch of the reel from one recording: reel time `start`–`end`,
    starting at recording time `clock`."""

    source: str  # CAMERA or SCREEN
    start: float
    end: float
    clock: float


def screen_windows(durations: list[float], *, hold_end: float = 0.0) -> list[tuple[float, float]]:
    """Reel-time spans (start, end) that show the screen, for segments of `durations`.

    `hold_end` keeps the last seconds of the reel on the camera (the end card).
    """
    total, at, windows = sum(durations), 0.0, []
    for i, length in enumerate(durations):
        start = at + SCREEN_LEAD
        end = min(at + length - SCREEN_TAIL, total - hold_end)
        if i > 0 and end - start >= MIN_SCREEN_SECONDS:
            windows.append((round(start, 3), round(end, 3)))
        at += length
    return windows


def plan_shots(cuts: list[Segment], windows: list[tuple[float, float]], *,
               offset: float) -> list[Shot]:
    """The reel's shots, in order. `cuts` are on the transcript's clock; `offset`
    moves them onto the recordings' clock (the trim offset)."""
    shots, at = [], 0.0
    for cut in cuts:
        end = at + cut.duration
        edges = [edge for window in windows if at < window[0] < window[1] < end
                 for edge in window]
        bounds = [at, *edges, end]
        for k in range(len(bounds) - 1):
            shots.append(Shot(SCREEN if k % 2 else CAMERA, round(bounds[k], 3),
                              round(bounds[k + 1], 3),
                              round(cut.start + offset + bounds[k] - at, 3)))
        at = end
    return shots


def transition_seconds(before: Shot, after: Shot) -> float:
    return DISSOLVE_SECONDS if before.source == after.source else SHRINK_SECONDS


def read_span(shots: list[Shot], k: int, fps: int) -> tuple[float, float]:
    """(start, length) to read from shot `k`'s recording, handles included."""
    shot = shots[k]
    before = transition_seconds(shots[k - 1], shot) / 2 if k else 0.0
    # One frame of slack: xfade needs its first input to last the whole overlap.
    after = transition_seconds(shot, shots[k + 1]) / 2 + 1 / fps if k + 1 < len(shots) else 0.0
    return round(shot.clock - before, 3), round(shot.end - shot.start + before + after, 3)


def join_graph(shots: list[Shot], pieces: list[str], out: str, *, size: tuple[int, int],
               camera_rect: Rect) -> str:
    """Chain `pieces` (one framed stream label per shot, all `size`) into `out`.

    `camera_rect` is where the presenter sits in a framed screen shot: the
    camera shrinks into it and grows back out of it.
    """
    if len(pieces) != len(shots) or not shots:
        raise FfmpegError("join_graph needs one framed piece per shot")
    if len(shots) == 1:
        return f"[{pieces[0]}]null[{out}]"
    full = (0.0, 0.0, float(size[0]), float(size[1]))
    chains, previous = [], pieces[0]
    for k in range(1, len(shots)):
        before, after = shots[k - 1], shots[k]
        seconds = transition_seconds(before, after)
        if before.source == after.source:
            options = "transition=fade"
        elif after.source == SCREEN:
            options = shrink_between(full, camera_rect, inner="a", outer="B")
        else:
            options = shrink_between(camera_rect, full, inner="b", outer="A")
        joined = out if k + 1 == len(shots) else f"j{k}"
        chains.append(f"[{previous}][{pieces[k]}]xfade={options}:duration={seconds}:"
                      f"offset={after.start - seconds / 2:.3f}[{joined}]")
        previous = joined
    return ";".join(chains)


def shrink_between(first: Rect, last: Rect, *, inner: str, outer: str) -> str:
    """xfade option string: one input, whole, in a rectangle moving `first` → `last`.

    `inner` is the input drawn in the rectangle ("a" outgoing, "b" incoming),
    sampled at the scaled position with a0…a2 / b0…b2; everywhere else shows
    `outer` ("A" or "B", the other input's own pixel). xfade's progress P runs
    1 → 0; the move is eased with a smoothstep.

    No st()/ld(): xfade evaluates the expression on several threads at once and
    those variables are shared, so the positions would turn to noise. Each term
    is spelled out instead. The inputs must be yuv444p (every plane full size).
    """
    ease = "((1-P)*(1-P)*(1+2*P))"  # smoothstep of 1-P

    def lerp(i: int) -> str:
        return f"({first[i]:.2f}{last[i] - first[i]:+.2f}*{ease})"

    x, y, w, h = (lerp(i) for i in range(4))
    at = f"(X-{x})*W/{w},(Y-{y})*H/{h}"
    sample = (f"if(eq(PLANE,0),{inner}0({at}),"
              f"if(eq(PLANE,1),{inner}1({at}),{inner}2({at})))")
    inside = f"gte(X,{x})*lt(X,{x}+{w})*gte(Y,{y})*lt(Y,{y}+{h})"
    return f"transition=custom:expr='if({inside},{sample},{outer})'"
