"""Read a webinar's replay transcript off Vimeo instead of the video pipeline.

Most webinars predate the video pipeline: there is no job, no ``frames_prefix``
and no transcript artefact in S3, so the answers spoken out loud in those
sessions have nothing to be recovered from. Their replays are on Vimeo, and
Vimeo has generated English captions for almost all of them — the same words the
pipeline would have transcribed, already sitting there.

This module turns ``webinars.video_embed_code`` into the cue list
``qa_extraction_service.extract_answers_from_cues`` expects. One consequence of
the source is worth naming: these captions are plain sentences with **no speaker
labels**, where the pipeline's own transcript is diarised. The model still fills
``answered_by`` on these rows, but it is inferring the name from what is said
(an introduction, a moderator addressing someone) rather than reading a label.
That holds up because these sessions have one host, who gives all but a handful
of the spoken answers — and where the transcript names nobody at all,
``qa_extraction_match.DEFAULT_SPEAKER`` records him rather than leaving the
speaker blank. The answer text is matched from the words themselves and is
unaffected.
"""

from __future__ import annotations

import logging

from src.content import vtt_parser
from src.integrations import vimeo
from src.workshops.models import Webinar

logger = logging.getLogger(__name__)


class TranscriptUnavailable(Exception):
    """No usable replay transcript for this webinar — the reason is the message.

    Every cause lands here (no embed code, no caption track, unparseable VTT)
    because callers treat them identically: this webinar cannot be filled, say
    why, move to the next one.
    """


def load_cues(webinar: Webinar, language: str = "en") -> list[dict]:
    """Vimeo's captions for this webinar's replay, as ``{start, end, text}`` cues.

    Raises:
        TranscriptUnavailable: when there is no replay, no caption track in
            ``language``, or the track does not parse.
    """
    raw_embed = (webinar.video_embed_code or "").strip()
    if not raw_embed:
        raise TranscriptUnavailable("Webinar has no replay video")

    try:
        video_ref = vimeo.extract_video_ref(raw_embed)
        content, track_name = vimeo.download_source_track(video_ref, language)
    except vimeo.VimeoError as exc:
        raise TranscriptUnavailable(str(exc)) from exc

    try:
        document = vtt_parser.parse(content)
    except vtt_parser.VttError as exc:
        raise TranscriptUnavailable(f"Caption track is not usable WebVTT: {exc}") from exc

    cues = [
        {"start": cue.start, "end": cue.end, "text": cue.text.strip()}
        for cue in document.cues
        if cue.text.strip()
    ]
    if not cues:
        raise TranscriptUnavailable("Caption track has no spoken text")

    logger.info(
        "Loaded replay transcript — webinar=%s video=%s track=%r cues=%d",
        webinar.id,
        video_ref,
        track_name,
        len(cues),
    )
    return cues
