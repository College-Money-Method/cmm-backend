"""Vimeo chapter markers — replace the whole list, then read it back.

Separate from ``vimeo.py`` only because that file is already long; the auth,
rate-limit retry and error mapping all come from it.

Replacement is whole-list on purpose. Chaptering reruns from the frames in S3,
so the previous list is never a partial result worth merging with — merging
would leave chapters from an earlier, worse classification sitting in the
timeline with no way to tell them apart.

Two endpoints exist for that: the batch endpoint documented as replace-all, and
the per-chapter create/delete pair. The batch endpoint could not be exercised
against a video this account owns during development, so a failure that means
"no such endpoint" (404/405) falls through to the per-chapter path rather than
failing the webinar. Both routes end at the same read-back.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from src.integrations.vimeo import VimeoError, _request

logger = logging.getLogger(__name__)

# Vimeo rejects a longer title outright, failing the whole publish for one
# chapter name. Measured against the live API rather than taken from the docs:
# a 50-character title is accepted and a 51-character one comes back 400 with
# `invalid_parameters: [{field: title}]`. Truncating loses a few words off the
# end of one chapter; not truncating loses the video's entire chapter list.
MAX_TITLE_CHARS = 50

# Statuses that mean the batch endpoint is not there, as opposed to the request
# being wrong or the token lacking rights — those must still fail loudly.
_ENDPOINT_ABSENT = (404, 405)


def _fit_title(title: str) -> str:
    """Bring a title inside Vimeo's limit, breaking on a word where possible.

    A hard cut at the limit reads like a transmission failure — "College Pricing
    & Fina" — so drop the partial word when doing that still leaves most of the
    title. A title with no space in range (one very long word) is cut anyway,
    because a rejected chapter is worse than an ugly one.
    """
    title = title.strip()
    if len(title) <= MAX_TITLE_CHARS:
        return title
    cut = title[:MAX_TITLE_CHARS]
    spaced = cut.rsplit(" ", 1)[0].rstrip(" ,;:-&")
    return spaced if len(spaced) >= MAX_TITLE_CHARS // 2 else cut


def _chapters_path(video_ref: str) -> str:
    return f"/videos/{video_ref}/chapters"


def _payload(chapters: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Reduce chapters to what Vimeo accepts: ascending, unique, titled.

    ``source`` and ``confidence`` are ours; sending them would be rejected as
    unknown fields. Duplicate timecodes are dropped rather than sent, because
    Vimeo fails the entire batch on one — and a duplicate can only mean two
    slides landed in the same second, where either title will do.
    """
    seen: set[int] = set()
    out: list[dict[str, object]] = []
    for chapter in sorted(chapters, key=lambda c: int(c["timecode"])):
        timecode = int(chapter["timecode"])
        if timecode in seen:
            logger.warning("dropping duplicate chapter at %ds: %s", timecode, chapter["title"])
            continue
        seen.add(timecode)
        out.append({"timecode": timecode, "title": _fit_title(str(chapter["title"]))})
    return out


def get_chapters(video_ref: str) -> list[dict]:
    """Current chapters on the video, oldest timecode first."""
    resp = _request("GET", _chapters_path(video_ref), params={"per_page": 100})
    data = resp.json().get("data") or []
    return sorted(data, key=lambda c: c.get("timecode", 0))


def _set_via_batch(video_ref: str, payload: list[dict[str, object]]) -> None:
    """Replace the list in one call. Raises VimeoError; 404/405 means try the other way."""
    _request("PUT", f"{_chapters_path(video_ref)}/batch", json=payload)


def _set_one_by_one(video_ref: str, payload: list[dict[str, object]]) -> None:
    """Delete what is there, then create each chapter.

    Not atomic: a failure part-way leaves the video with fewer chapters than it
    started with. That is acceptable because the read-back below will catch it
    and fail the job, and a rerun replaces the list wholesale anyway.
    """
    path = _chapters_path(video_ref)
    for existing in get_chapters(video_ref):
        uri = existing.get("uri")
        if uri:
            _request("DELETE", uri)
    for chapter in payload:
        _request("POST", path, json=chapter)


def set_chapters(video_ref: str, chapters: Sequence[Mapping[str, object]]) -> list[dict]:
    """Make the video's chapter list exactly ``chapters``; return what Vimeo has.

    Raises:
        VimeoError: on any API failure, or when the read-back does not match
            what was sent. A silent mismatch would publish a page whose chapter
            menu disagrees with the one recorded on the job.
    """
    payload = _payload(chapters)
    if not payload:
        raise VimeoError("Refusing to publish a video with no chapters")

    try:
        _set_via_batch(video_ref, payload)
    except VimeoError as exc:
        if exc.status not in _ENDPOINT_ABSENT:
            raise
        logger.info(
            "Vimeo chapters batch endpoint answered %s — falling back to per-chapter writes",
            exc.status,
        )
        _set_one_by_one(video_ref, payload)

    confirmed = get_chapters(video_ref)
    if len(confirmed) != len(payload):
        raise VimeoError(
            f"Vimeo kept {len(confirmed)} of {len(payload)} chapters on video {video_ref}"
        )
    return confirmed
