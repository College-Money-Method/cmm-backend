"""Vimeo API client — text track (closed caption) management.

Auth is a personal access token (settings.vimeo_access_token) with the
``public private edit`` scopes. Vimeo evaluates permissions against the token
owner's *team role*, not video ownership, so a team member with edit rights can
manage tracks on videos owned by the team account.

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
        return (
            "Vimeo denied access (403). Your token's account does not have edit "
            f"rights on this video. {detail}".strip()
        )
    if resp.status_code == 401:
        return "Vimeo rejected the access token (401). It may have been revoked."
    return f"Vimeo API error {resp.status_code} on {path}: {detail}"


# Our internal locale codes (src.config.SUPPORTED_LOCALES) do not always match
# Vimeo's text-track codes. Candidates are tried in order against Vimeo's live
# list; the first that exists wins. Listed explicitly rather than prefix-matched
# because a "zh" prefix would happily match "zh-Hant" (Traditional) when the
# caller asked for Mandarin Simplified.
# Only locales whose exact code is absent or ambiguous on Vimeo need an entry —
# everything else falls through to an exact match. "zh" is the ambiguous one:
# Vimeo's bare "zh" is generic "Chinese", so Mandarin is pinned to Simplified.
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


def get_video_name(video_ref: str) -> str:
    """Return the video's title — also serves as an existence + access check.

    Raises:
        VimeoError: 404 when the video does not exist or is invisible to the token.
    """
    resp = _request("GET", f"/videos/{video_ref}", params={"fields": "name"})
    return resp.json().get("name") or f"Video {video_ref.split(':')[0]}"


def list_text_tracks(video_ref: str) -> list[dict]:
    """Return existing text tracks: ``[{uri, language, name, type}, ...]``."""
    resp = _request(
        "GET",
        f"/videos/{video_ref}/texttracks",
        params={"fields": "uri,language,name,type,active"},
    )
    return resp.json().get("data") or []


def delete_text_track(track_uri: str) -> None:
    """Delete a text track by its API uri (e.g. ``/videos/123/texttracks/456``)."""
    _request("DELETE", track_uri)


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
