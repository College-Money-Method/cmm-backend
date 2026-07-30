"""Vimeo API client — text track (closed caption) management.

Auth is a personal access token (settings.vimeo_access_token) carrying the
scopes in ``REQUIRED_SCOPES`` below. Scopes are fixed at token creation, so a
missing one means generating a new token.

Permissions are evaluated against the token owner's *team role*, not video
ownership, so a team member with edit rights can manage tracks on videos owned
by the team account.

Text track upload is a two-step flow:
  1. POST /videos/{id}/texttracks  → creates the track record, returns an
     upload ``link`` (a pre-signed URL, no auth header of its own).
  2. PUT <link> with the WebVTT bytes → the track becomes usable.

A track that is created but never uploaded to sits broken on the video, so
step 2 failures trigger a best-effort delete of the orphan.
"""

from __future__ import annotations

import logging
import re

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://api.vimeo.com"
_ACCEPT = "application/vnd.vimeo.*+json;version=3.4"

# Every scope this module needs, by call:
#   public, private  → GET /videos/{id}, GET .../texttracks
#   edit + upload    → POST .../texttracks (Vimeo counts writing the caption
#                      FILE as an upload, so 'edit' alone 403s)
#   delete           → DELETE .../texttracks/{id}, used when replacing a track
# Scopes are fixed when a personal access token is generated and cannot be
# widened later — a missing one means regenerating the token.
REQUIRED_SCOPES = "public private edit upload delete"

# Vimeo API is slow on writes; uploads of a large VTT can take a few seconds.
_TIMEOUT = 30.0

# Video reference forms we accept from the admin form:
#   https://vimeo.com/123456789
#   https://vimeo.com/123456789/abcdef0123          (unlisted, hash as path segment)
#   https://player.vimeo.com/video/123456789?h=abc  (embed src / full iframe snippet)
#   123456789
# The privacy hash matters: unlisted videos are addressed as "{id}:{hash}".
_ID_PATTERNS = (
    re.compile(r"player\.vimeo\.com/video/(?P<id>\d+)(?:\?[^\"'\s]*\bh=(?P<hash>[0-9a-zA-Z]+))?"),
    re.compile(r"vimeo\.com/(?P<id>\d+)(?:/(?P<hash>[0-9a-zA-Z]+))?"),
    re.compile(r"^\s*(?P<id>\d+)(?::(?P<hash>[0-9a-zA-Z]+))?\s*$"),
)


class VimeoError(Exception):
    """Raised when a Vimeo API call fails or the input is unusable.

    ``status`` carries the upstream HTTP status when there was one, so callers
    can distinguish 404 (video not found) from 403 (no edit permission).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def extract_video_ref(raw: str) -> str:
    """Parse an admin-supplied video URL / embed code / id into an API reference.

    Returns ``"123456789"`` or ``"123456789:privacyhash"`` (unlisted videos).

    Raises:
        VimeoError: when no video id can be found in the input.
    """
    if not raw or not raw.strip():
        raise VimeoError("Video URL or ID is required")

    for pattern in _ID_PATTERNS:
        match = pattern.search(raw)
        if match:
            video_id = match.group("id")
            privacy_hash = match.groupdict().get("hash")
            return f"{video_id}:{privacy_hash}" if privacy_hash else video_id

    raise VimeoError(
        "Could not find a Vimeo video ID in that input. Paste the video URL, "
        "the embed code, or the numeric ID."
    )


def _headers() -> dict[str, str]:
    if not settings.vimeo_access_token:
        raise VimeoError("VIMEO_ACCESS_TOKEN is not configured on the server")
    return {
        "Authorization": f"bearer {settings.vimeo_access_token}",
        "Accept": _ACCEPT,
    }


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Issue an authenticated Vimeo API call, mapping failures to VimeoError."""
    url = f"{_API_BASE}{path}"
    try:
        resp = httpx.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)
    except httpx.TimeoutException as exc:
        raise VimeoError(f"Vimeo request timed out: {method} {path}") from exc
    except httpx.HTTPError as exc:
        raise VimeoError(f"Could not reach Vimeo: {exc}") from exc

    if resp.status_code >= 400:
        raise VimeoError(_describe_error(resp, path), status=resp.status_code)
    return resp


def _describe_error(resp: httpx.Response, path: str) -> str:
    """Turn a Vimeo error response into a message an admin can act on."""
    try:
        body = resp.json()
        detail = body.get("developer_message") or body.get("error") or ""
    except Exception:
        detail = resp.text[:200]

    if resp.status_code == 404:
        return "Video not found on Vimeo, or your account cannot see it."
    if resp.status_code == 403:
        # Two very different causes share this status: a missing token SCOPE
        # (fixable by regenerating the token) versus insufficient role/permission
        # on the video (needs a team-role change). Vimeo names the scope in
        # developer_message, so lead with that rather than guessing.
        if "scope" in detail.lower():
            return (
                f"Vimeo denied access (403): {detail.rstrip('.')}. Regenerate the "
                f"personal access token at developer.vimeo.com with all of: "
                f"{REQUIRED_SCOPES}."
            )
        return (
            "Vimeo denied access (403). Your account does not have edit rights on "
            f"this video. {detail}".strip()
        )
    if resp.status_code == 401:
        return "Vimeo rejected the access token (401). It may have been revoked."
    return f"Vimeo API error {resp.status_code} on {path}: {detail}"


# Our internal locale codes (src.config.SUPPORTED_LOCALES) do not always match
# Vimeo's text-track codes. Candidates are tried in order against Vimeo's live
# list; the first that exists wins. Only locales whose exact code is absent or
# ambiguous on Vimeo need an entry — everything else falls through to an exact
# match. "zh" is the ambiguous one: Vimeo's bare "zh" is generic "Chinese", so
# Mandarin is pinned to Simplified rather than prefix-matching into "zh-Hant".
_LANGUAGE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "zh": ("zh-Hans", "zh-CN", "zh"),
    "zh-Hant": ("zh-Hant", "zh-TW"),
}

# Vimeo's supported text-track languages, fetched once per process.
_language_cache: dict[str, str] | None = None


def _fetch_languages() -> dict[str, str]:
    """Return Vimeo's text-track languages as ``{code: display_name}`` (cached)."""
    global _language_cache
    if _language_cache is not None:
        return _language_cache

    resp = _request("GET", "/languages", params={"filter": "texttracks", "per_page": 100})
    _language_cache = {
        item["code"]: item.get("name") or item["code"]
        for item in resp.json().get("data") or []
        if item.get("code")
    }
    return _language_cache


def resolve_language(locale: str, fallback_name: str) -> tuple[str, str]:
    """Map an internal locale to a ``(vimeo_code, display_name)`` pair.

    Raises:
        VimeoError: when Vimeo supports no equivalent of the requested locale.
    """
    try:
        languages = _fetch_languages()
    except VimeoError:
        # Language list unavailable (e.g. transient 5xx) — fall back to sending
        # the locale through as-is rather than failing the whole job here.
        logger.warning("Could not fetch Vimeo language list; using '%s' verbatim", locale)
        return locale, fallback_name

    for candidate in _LANGUAGE_CANDIDATES.get(locale, (locale,)):
        if candidate in languages:
            return candidate, languages[candidate]

    raise VimeoError(f"Vimeo does not offer a caption language matching '{locale}'.")


def missing_scopes() -> list[str]:
    """Return required scopes the configured token lacks (empty when fine).

    Cheap pre-flight: a caption job otherwise fails halfway, after paying for
    translation, on a 403 that only names one missing scope at a time.
    """
    resp = _request("GET", "/oauth/verify")
    granted = set((resp.json().get("scope") or "").split())
    return [s for s in REQUIRED_SCOPES.split() if s not in granted]


def get_video_name(video_ref: str) -> str:
    """Return the video's title — also serves as an existence + access check.

    Raises:
        VimeoError: 404 when the video does not exist or is invisible to the token.
    """
    resp = _request("GET", f"/videos/{video_ref}", params={"fields": "name"})
    return resp.json().get("name") or f"Video {video_ref.split(':')[0]}"


def list_text_tracks(
    video_ref: str, fields: str = "uri,language,name,type,active"
) -> list[dict]:
    """Return existing text tracks: ``[{uri, language, name, type}, ...]``.

    ``fields`` is widened by callers that also need the pre-signed ``link``.
    """
    resp = _request(
        "GET", f"/videos/{video_ref}/texttracks", params={"fields": fields}
    )
    return resp.json().get("data") or []


def delete_text_track(track_uri: str) -> None:
    """Delete a text track by its API uri (e.g. ``/videos/123/texttracks/456``)."""
    _request("DELETE", track_uri)


def download_source_track(video_ref: str, language: str = "en") -> tuple[str, str]:
    """Fetch an existing track's WebVTT content to use as the translation source.

    Lets an admin translate Vimeo's own (AI or uploaded) English captions without
    re-uploading a transcript by hand.

    Selection is deterministic: a video can carry several tracks in the same
    language, so the ``active`` one wins — that is what the player actually shows
    — falling back to the first listed.

    Returns:
        ``(vtt_content, track_name)``.

    Raises:
        VimeoError: when the video has no track in ``language``, or the download
            fails / returns something that is not WebVTT.
    """
    candidates = [
        t
        for t in list_text_tracks(video_ref, fields="uri,language,name,type,active,link")
        if (t.get("language") or "").lower().startswith(language.lower())
    ]
    if not candidates:
        raise VimeoError(
            f"This video has no {language} caption track to translate from. "
            "Generate one on Vimeo first, or upload a transcript file instead."
        )

    track = next((t for t in candidates if t.get("active")), candidates[0])
    link = track.get("link")
    if not link:
        raise VimeoError(
            "Vimeo did not return a download link for the existing caption track."
        )

    # Pre-signed URL — sending the Authorization header would be rejected.
    try:
        resp = httpx.get(link, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise VimeoError(f"Could not download the existing caption track: {exc}") from exc

    content = resp.text
    if not content.lstrip("﻿").startswith("WEBVTT"):
        raise VimeoError(
            "The existing caption track did not come back as WebVTT — upload a "
            "transcript file instead."
        )

    return content, track.get("name") or f"{language} captions"


def upload_text_track(
    video_ref: str, language: str, name: str, content: str, *, active: bool = True
) -> str:
    """Create a subtitle track and upload its WebVTT content.

    Args:
        video_ref: "123456789" or "123456789:privacyhash".
        language: Vimeo language code (see ``GET /languages?filter=texttracks``).
        name: Display label shown in the player's caption menu.
        content: WebVTT document.
        active: Whether the track is selectable/enabled in the player.

    Returns:
        The created track's API uri.

    Raises:
        VimeoError: on create or upload failure. An orphaned track (created but
            with no content uploaded) is deleted before the error propagates.
    """
    create = _request(
        "POST",
        f"/videos/{video_ref}/texttracks",
        json={"type": "subtitles", "language": language, "name": name, "active": active},
    ).json()

    track_uri = create.get("uri")
    upload_link = create.get("link")
    if not upload_link:
        raise VimeoError(
            f"Vimeo created the {language} track but returned no upload link — cannot attach the file."
        )

    # The upload link is pre-signed: it must NOT carry the Authorization header.
    try:
        put = httpx.put(
            upload_link,
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/vtt"},
            timeout=_TIMEOUT,
        )
        put.raise_for_status()
    except Exception as exc:
        _delete_orphan(track_uri, language)
        raise VimeoError(f"Uploading the {language} caption file to Vimeo failed: {exc}") from exc

    return track_uri or ""


def _delete_orphan(track_uri: str | None, language: str) -> None:
    """Best-effort cleanup of a created-but-empty track. Never raises."""
    if not track_uri:
        return
    try:
        delete_text_track(track_uri)
        logger.info("Removed orphaned %s text track %s after upload failure", language, track_uri)
    except Exception as exc:
        logger.warning("Could not remove orphaned text track %s: %s", track_uri, exc)
