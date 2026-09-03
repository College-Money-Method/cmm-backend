"""Find where the presentation actually starts.

The dead opening is not silent — the presenter talks through it ("we'll wait for
families to come in"), so it produces transcript cues like any other speech and
silence detection cannot find the boundary. The real start is semantic, so one
cheap text call picks the cue where the presentation proper begins.

Every guard here is asymmetric on purpose: trimming too late destroys content,
while leaving dead air only looks sloppy. So the answer must land on a real cue
inside a bounded window, and any failure falls back to no trim at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.ffmpeg_ops import TRIM_LEAD_IN_SECONDS
from src.video_pipeline.transcript import Cue, format_timestamp

logger = logging.getLogger(__name__)

# How much transcript the model sees, and how late the answer may fall.
HEAD_WINDOW_SECONDS = 600.0
MAX_TRIM_SECONDS = 900.0

_SYSTEM = """\
You analyse the opening transcript of a recorded webinar for families about \
college financial aid.

The recording starts before the session does. During that opening the host is \
present but stalling: greeting early arrivals, asking people to wait, mentioning \
that they will start soon, making small talk, or checking whether attendees can \
hear. The presentation proper begins when the host stops stalling and starts \
delivering — introducing themselves or the session, stating what will be covered, \
or moving into the first topic.

You are given numbered transcript cues. Identify the FIRST cue that belongs to the \
presentation proper.

Reply with only a JSON object:
{"cue_index": <integer>, "reason": "<short quote or explanation>"}

Rules:
- cue_index must be one of the numbers shown.
- If the presentation appears to start immediately, answer 0.
- Prefer an earlier cue when uncertain. Cutting real content is much worse than \
leaving a few seconds of stalling in.\
"""


@dataclass(frozen=True)
class TrimPoint:
    offset: float          # seconds to cut from the head, lead-in already applied
    cue_index: int         # index into the cue list the model chose
    cue_start: float       # that cue's raw start time
    reason: str
    fallback: bool         # True when detection failed and we defaulted to no trim
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "cue_index": self.cue_index,
            "cue_start": self.cue_start,
            "reason": self.reason,
            "fallback": self.fallback,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _no_trim(reason: str) -> TrimPoint:
    return TrimPoint(
        offset=0.0, cue_index=0, cue_start=0.0, reason=reason, fallback=True
    )


def build_prompt(cues: list[Cue]) -> str:
    """Render the head cues as a numbered, timestamped list."""
    return "\n".join(
        f"[{i}] {format_timestamp(cue.start)} {cue.text}"
        for i, cue in enumerate(cues)
    )


def detect_trim_point(cues: list[Cue]) -> TrimPoint:
    """Pick the cue where the presentation starts and derive a trim offset."""
    if not cues:
        return _no_trim("no transcript cues available")

    head = [cue for cue in cues if cue.start <= HEAD_WINDOW_SECONDS] or cues[:1]
    try:
        parsed, in_tok, out_tok = call_json(
            system=_SYSTEM, content=build_prompt(head), max_tokens=300
        )
    except BedrockCallError as exc:
        logger.warning("trim-point detection failed, publishing untrimmed: %s", exc)
        return _no_trim(f"detection failed: {exc}")

    raw_index = parsed.get("cue_index")
    reason = str(parsed.get("reason") or "").strip()

    # The answer must name a real cue — this is what keeps the offset on a
    # boundary the transcript actually contains.
    if not isinstance(raw_index, int) or isinstance(raw_index, bool):
        logger.warning("trim-point reply had no integer cue_index: %r", parsed)
        return _no_trim(f"unusable reply: {parsed!r}")
    if not 0 <= raw_index < len(head):
        logger.warning(
            "trim-point cue_index %s outside 0..%d", raw_index, len(head) - 1
        )
        return _no_trim(f"cue_index {raw_index} out of range")

    cue_start = head[raw_index].start
    if cue_start > MAX_TRIM_SECONDS:
        logger.warning(
            "trim-point %.1fs beyond the %.0fs bound; publishing untrimmed",
            cue_start,
            MAX_TRIM_SECONDS,
        )
        return _no_trim(f"chosen cue at {cue_start:.1f}s exceeds bound")

    offset = max(0.0, cue_start - TRIM_LEAD_IN_SECONDS)
    logger.info(
        "trim point: cue %d at %s (offset %.2fs) — %s",
        raw_index,
        format_timestamp(cue_start),
        offset,
        reason or "no reason given",
    )
    return TrimPoint(
        offset=offset,
        cue_index=raw_index,
        cue_start=cue_start,
        reason=reason,
        fallback=False,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
