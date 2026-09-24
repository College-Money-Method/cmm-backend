"""TikTok-style captions for a trailer reel, written as an ASS subtitle script.

ASS is what ffmpeg's ``subtitles`` filter renders through libass, which already
ships in the ffmpeg build — so moving, word-by-word captions need no caption
library or browser renderer, only this file. The look, in the site's brand:

* words appear in short groups on one line, white Inter ExtraBold on a solid
  cmm-teal bar, so they read over any frame;
* the word being spoken turns cmm-flax;
* on a portrait screen shot the presenter sits under the slide, right where
  the captions go, so there they move up onto a teal band between the two;
* a teal title card in Lora, the site's heading face, sits at the top for the first seconds, and an optional call
  to action for the last seconds;
* a teal lower-third strip along the bottom edge names the presenter. It also
  covers the name tag and clock Zoom burns into the corners of its recording,
  a clock that would otherwise jump at every cut;
* a thin sea-glass progress bar runs along the top of that strip.

One ASS event is written per spoken word, each re-drawing its whole group with
that word highlighted. That is the standard way to get per-word styling out of
libass: events are cheap, and nothing moves that the timings do not move.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.video_pipeline.trailer_render import (
    PORTRAIT_CAPTION_BAND,
    PORTRAIT_SLIDE_HEIGHT,
    PORTRAIT_SLIDE_TOP,
)
from src.video_pipeline.trailer_words import Word

# A pause this long starts a new group even when the current one has room.
GROUP_BREAK_GAP = 0.45
# Gaps shorter than this between groups are bridged, so captions do not flicker.
BRIDGE_GAP = 0.35
TITLE_SECONDS = 3.5
CTA_SECONDS = 3.0
# Both ship in fonts/: the site sets body text in Inter and headings in Lora.
CAPTION_FONT = "Inter ExtraBold"
HEADING_FONT = "Lora"

# ASS colours are &HAABBGGRR.
WHITE = "&H00FFFFFF"
BRAND_TEAL = "&H008D784F"  # #4F788D, cmm-teal
FLAX = "&H007ADAF7"  # #F7DA7A, cmm-flax
SEA_GLASS = "&H00C0C8B0"  # #B0C8C0, cmm-sea-glass


@dataclass(frozen=True)
class Layout:
    """Text placement for one orientation, in the script's own coordinates.

    libass scales PlayRes coordinates to whatever size the video is rendered at,
    so a landscape reel can stay at the source's native 1280x720.
    """

    width: int
    height: int
    caption_size: int
    caption_margin_v: int  # caption baseline, up from the bottom edge
    title_size: int
    title_margin_v: int  # title card, down from the top edge
    side_margin: int
    max_words: int
    max_chars: int
    strip_height: int  # tall enough to cover Zoom's corner overlays in landscape
    band_centre: int = 0  # captions' centre on screen shots; 0 keeps them in place

    @property
    def bar_height(self) -> int:
        return max(4, round(self.height * 0.006))


PORTRAIT = Layout(1080, 1920, caption_size=85, caption_margin_v=560, title_size=64,
                  title_margin_v=230, side_margin=90, max_words=3, max_chars=18,
                  strip_height=110,
                  band_centre=PORTRAIT_SLIDE_TOP + PORTRAIT_SLIDE_HEIGHT + PORTRAIT_CAPTION_BAND // 2)
LANDSCAPE = Layout(1920, 1080, caption_size=68, caption_margin_v=120, title_size=58,
                   title_margin_v=70, side_margin=200, max_words=5, max_chars=30,
                   strip_height=76)
LAYOUTS = {"portrait": PORTRAIT, "landscape": LANDSCAPE}


def _header(layout: Layout) -> str:
    # BorderStyle 3 draws the outline as a box: this is the bar's padding. libass
    # boxes each styled run apart, so the highlighted word's box overlaps its
    # neighbours'; the bar must stay fully opaque or the overlaps show as seams.
    bar_pad = round(layout.caption_size / 5)
    strip_size = round(layout.strip_height * 0.42)
    strip_margin_v = (layout.strip_height - strip_size) // 2
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {layout.width}
PlayResY: {layout.height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, \
Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{CAPTION_FONT},{layout.caption_size},{WHITE},{WHITE},{BRAND_TEAL},{BRAND_TEAL},\
-1,0,0,0,100,100,2,0,3,{bar_pad},0,2,{layout.side_margin},{layout.side_margin},{layout.caption_margin_v},1
Style: Title,{HEADING_FONT},{layout.title_size},{WHITE},{WHITE},{BRAND_TEAL},{BRAND_TEAL},-1,0,0,0,100,\
100,0,0,3,{layout.title_size // 2.5:.0f},0,8,{layout.side_margin},{layout.side_margin},\
{layout.title_margin_v},1
Style: Strip,{HEADING_FONT},{strip_size},{WHITE},{WHITE},{BRAND_TEAL},&H00000000,-1,0,0,0,100,100,1,0,\
1,0,0,1,{layout.side_margin // 2},{layout.side_margin // 2},{strip_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass(frozen=True)
class _Group:
    words: list[Word]
    start: float
    end: float


def build_ass(
    words: list[Word],
    *,
    duration: float,
    layout: Layout,
    title: str = "",
    cta: str = "",
    presenter: str = "",
    screen: list[tuple[float, float]] = (),
) -> str:
    """The complete ASS script for a reel of `duration` seconds.

    `presenter` is the lower-third text, e.g. "Paul Martin · College Money Method".
    `screen` holds the reel-time spans that show the shared screen.
    """
    lines = [_header(layout), *_strip(duration, layout, presenter)]
    if title:
        lines.append(_event(0.0, min(TITLE_SECONDS, duration), "Title",
                            r"{\fad(150,250)}" + _escape(title), layer=1))
    if cta:
        lines.append(_event(max(0.0, duration - CTA_SECONDS), duration, "Title",
                            r"{\fad(250,0)}" + _escape(cta), layer=1))
    for group in group_words(words, duration=duration, layout=layout):
        lines.extend(_group_events(group, layout, screen))
    return "\n".join(lines) + "\n"


def group_words(words: list[Word], *, duration: float, layout: Layout) -> list[_Group]:
    """Split words into short on-screen groups and settle when each one shows."""
    groups: list[list[Word]] = []
    for word in words:
        current = groups[-1] if groups else None
        if current is not None and _fits(current, word, layout):
            current.append(word)
        else:
            groups.append([word])

    timed: list[_Group] = []
    for i, group in enumerate(groups):
        end = group[-1].end
        if i + 1 < len(groups):
            next_start = groups[i + 1][0].start
            if next_start - end < BRIDGE_GAP:
                end = next_start
        timed.append(_Group(words=group, start=group[0].start, end=min(end, duration)))
    return timed


def _fits(group: list[Word], word: Word, layout: Layout) -> bool:
    if len(group) >= layout.max_words:
        return False
    if word.start - group[-1].end > GROUP_BREAK_GAP:
        return False
    if group[-1].text.endswith((".", "?", "!", ",")):
        return False
    chars = sum(len(w.text) for w in group) + len(group) + len(word.text)
    return chars <= layout.max_chars


def _group_events(group: _Group, layout: Layout, screen: list[tuple[float, float]]) -> list[str]:
    events = []
    for i, word in enumerate(group.words):
        start = group.start if i == 0 else word.start
        end = group.words[i + 1].start if i + 1 < len(group.words) else group.end
        if end <= start:
            continue
        parts = []
        for j, other in enumerate(group.words):
            text = _escape(other.text)
            if j == i:
                parts.append(rf"{{\c{FLAX}&}}{text}{{\r}}")
            else:
                parts.append(text)
        text = " ".join(parts)
        if not layout.band_centre:
            events.append(_event(start, end, "Caption", text))
            continue
        # Split where the picture switches between camera and screen, so a
        # caption showing across the switch moves with the picture.
        edges = sorted(e for window in screen for e in window if start < e < end)
        band = rf"{{\an5\pos({layout.width // 2},{layout.band_centre})}}"
        for a, b in zip([start, *edges], [*edges, end]):
            on_screen = any(w0 <= a < w1 for w0, w1 in screen)
            events.append(_event(a, b, "Caption", (band if on_screen else "") + text))
    return events


def _strip(duration: float, layout: Layout, presenter: str) -> list[str]:
    """The lower-third strip, its progress bar, and the presenter's name on it."""
    w, h, top = layout.width, layout.strip_height, layout.height - layout.strip_height
    events = [
        _event(0.0, duration, "Strip", _box(0, top, w, h, BRAND_TEAL), layer=2),
        # Fills left to right over the reel: \an7 scales from the top-left corner.
        _event(0.0, duration, "Strip",
               rf"{{\fscx0\t(0,{round(duration * 1000)},\fscx100)}}"
               + _box(0, top, w, layout.bar_height, SEA_GLASS), layer=3),
    ]
    if presenter:
        events.append(_event(0.0, duration, "Strip", _escape(presenter), layer=3))
    return events


def _box(x: int, y: int, w: int, h: int, colour: str) -> str:
    """An ASS vector drawing of a filled rectangle with its top-left at (x, y)."""
    return (rf"{{\an7\pos({x},{y})\bord0\shad0\c{colour}&\p1}}"
            rf"m 0 0 l {w} 0 {w} {h} 0 {h}")


def _event(start: float, end: float, style: str, text: str, *, layer: int = 0) -> str:
    return f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}"


def _ts(seconds: float) -> str:
    """ASS time: h:mm:ss.cc (centiseconds)."""
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape(text: str) -> str:
    """Neutralise the characters ASS reads as override-tag syntax."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")
