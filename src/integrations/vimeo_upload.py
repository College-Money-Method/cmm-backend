"""Vimeo upload, transcode wait, and embed-domain whitelisting.

Separate from ``vimeo.py`` (which owns auth, rate-limit retry and text tracks)
because this is a different protocol: the video bytes go over **tus**, not the
JSON API. No tus client is in the dependency set, and the resumable subset
Vimeo needs is small enough that pulling one in would be more code than this.

Privacy here is *embed-only*, not unlisted:

    {"view": "disable", "embed": "whitelist"}

``view: disable`` hides the video everywhere on vimeo.com, and
``embed: whitelist`` restricts the player to registered domains. A whitelist
with **zero** registered domains blocks the player everywhere, production
included — so registering the domains is part of publishing a video, not a
follow-up step, and ``create_video`` does it before returning.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from src.app_config import operator_settings
from src.config import settings
from src.integrations.vimeo import VimeoError, _request

logger = logging.getLogger(__name__)

# tus chunk size. Large enough that a 1-2 GB file is a few hundred PATCHes,
# small enough that one failed chunk is cheap to repeat.
_CHUNK_BYTES = 64 * 1024 * 1024
_UPLOAD_TIMEOUT = 600.0
_TUS_RESUMABLE = "1.0.0"

# Transcode poll cadence. Vimeo has no webhook we can receive here, so this is a
# poll; the interval is a compromise between latency and the 50 req/min token
# budget shared with every other Vimeo call in the app.
_POLL_INTERVAL_SECONDS = 15.0

EMBED_ONLY_PRIVACY = {"view": "disable", "embed": "whitelist"}


def embed_domains() -> list[str]:
    """Domains to register on each uploaded video's whitelist, from config."""
    return [part.strip() for part in settings.vimeo_embed_domains.split(",") if part.strip()]


def allow_embed_domain(video_ref: str, domain: str) -> None:
    """Register one domain on a video's embed whitelist.

    Raises VimeoError — a video whose whitelist is empty plays nowhere, so a
    failure here is a publishing failure, not a warning.
    """
    _request("PUT", f"/videos/{video_ref}/privacy/domains/{domain}")
    logger.info("Vimeo embed domain registered — video=%s domain=%s", video_ref, domain)


def upload_owner_path() -> str:
    """Collection a new video is created in.

    ``/me/videos`` is the token owner's own library. A configured
    ``vimeo_upload_user_uri`` puts the video in that account's library instead,
    which is what keeps replays in the team library rather than in whichever
    personal account issued the token.
    """
    owner = settings.vimeo_upload_user_uri.strip().rstrip("/")
    return f"{owner}/videos" if owner else "/me/videos"


def audit_folder_uri() -> str:
    """Folder audit runs are filed into, or "" when none is configured.

    The admin-set value in Global Settings wins over the env seed, so an
    operator can send the next audit somewhere else without a deploy. A blank
    override is a cleared override, not a configured empty folder — it falls
    back to the seed, which is what makes the field clearable.
    """
    override = operator_settings.vimeo_audit_folder_uri()
    configured = (override or "").strip() or settings.vimeo_audit_folder_uri
    return configured.strip().rstrip("/")


def _create_upload_record(
    name: str, size: int, description: str | None, folder_uri: str | None
) -> dict:
    body: dict[str, object] = {
        "upload": {"approach": "tus", "size": str(size)},
        "name": name,
        "privacy": dict(EMBED_ONLY_PRIVACY),
    }
    if description:
        body["description"] = description
    if folder_uri:
        body["folder_uri"] = folder_uri

    path = upload_owner_path()
    try:
        return _request(
            "POST",
            path,
            json=body,
            params={"fields": "uri,link,player_embed_url,upload"},
        ).json()
    except VimeoError as exc:
        if path == "/me/videos":
            raise
        # Vimeo scopes upload permission to the API app, not the token: a token
        # held by a team member still cannot create a video in the team's
        # library unless the app itself belongs to that account. The raw
        # message ("This app can only upload to the app owner's account") does
        # not say what to change, so say it here.
        raise VimeoError(
            f"Vimeo refused to create a video under {settings.vimeo_upload_user_uri}: {exc}. "
            "VIMEO_ACCESS_TOKEN must come from an API app owned by (or OAuth-authorised "
            "on) that account, with scopes 'public private edit upload delete'."
        ) from exc


def _upload_bytes(upload_link: str, path: Path, size: int) -> None:
    """PATCH the file to the tus endpoint in chunks, resuming from the server's offset.

    The tus link is pre-signed and must not carry the API Authorization header.
    Each response reports the new ``Upload-Offset``; trusting that rather than a
    locally tracked counter is what makes a retried chunk safe.
    """
    offset = 0
    with path.open("rb") as handle:
        while offset < size:
            handle.seek(offset)
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            try:
                resp = httpx.patch(
                    upload_link,
                    content=chunk,
                    headers={
                        "Tus-Resumable": _TUS_RESUMABLE,
                        "Upload-Offset": str(offset),
                        "Content-Type": "application/offset+octet-stream",
                    },
                    timeout=_UPLOAD_TIMEOUT,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise VimeoError(f"Vimeo upload failed at offset {offset}: {exc}") from exc

            new_offset = int(resp.headers.get("Upload-Offset") or 0)
            if new_offset <= offset:
                raise VimeoError(
                    f"Vimeo upload made no progress at offset {offset} "
                    f"(server reported {new_offset})"
                )
            offset = new_offset
            logger.debug("Vimeo upload progress — %d/%d bytes", offset, size)

    if offset < size:
        raise VimeoError(f"Vimeo upload ended early: {offset} of {size} bytes")


def create_video(
    path: Path,
    name: str,
    *,
    description: str | None = None,
    folder_uri: str | None = None,
) -> dict:
    """Upload ``path`` as an embed-only video and whitelist the configured domains.

    ``folder_uri`` files the video into a Vimeo folder ("project") as part of
    creating it. Set at creation rather than moved afterwards: moving is a
    second permission Vimeo grants separately, so a video created loose can end
    up stranded outside the folder it was meant for.

    Returns ``{"video_ref", "video_id", "hash", "player_embed_url"}``.

    The whitelist registration happens before the return, and a failure there
    raises: an embed-only video with an empty whitelist is invisible everywhere,
    which is a worse outcome than no video at all because it looks published.
    """
    size = path.stat().st_size
    if size <= 0:
        raise VimeoError(f"Refusing to upload an empty file: {path}")

    created = _create_upload_record(name, size, description, folder_uri)
    uri = created.get("uri") or ""
    video_id = uri.rsplit("/", 1)[-1]
    if not video_id:
        raise VimeoError("Vimeo did not return a video URI for the upload")

    upload_link = (created.get("upload") or {}).get("upload_link")
    if not upload_link:
        raise VimeoError(f"Vimeo returned no tus upload link for video {video_id}")

    _upload_bytes(upload_link, path, size)
    logger.info("Vimeo upload complete — video=%s bytes=%d", video_id, size)

    player_embed_url = created.get("player_embed_url") or ""
    privacy_hash = _hash_from_embed_url(player_embed_url)
    video_ref = f"{video_id}:{privacy_hash}" if privacy_hash else video_id

    for domain in embed_domains():
        allow_embed_domain(video_ref, domain)

    return {
        "video_ref": video_ref,
        "video_id": video_id,
        "hash": privacy_hash,
        "player_embed_url": player_embed_url,
    }


def _hash_from_embed_url(player_embed_url: str) -> str:
    """Pull the ``h=`` privacy hash out of a player URL, if there is one.

    Whether an embed-only video carries a hash is Vimeo's decision, not ours, so
    it is read back from what Vimeo returned rather than assumed either way.
    """
    if not player_embed_url or "h=" not in player_embed_url:
        return ""
    from urllib.parse import parse_qs, urlparse

    return (parse_qs(urlparse(player_embed_url).query).get("h") or [""])[0]


def get_transcode_status(video_ref: str) -> tuple[str, str]:
    """Return ``(transcode_status, upload_status)`` for a video."""
    body = _request(
        "GET", f"/videos/{video_ref}", params={"fields": "transcode.status,upload.status"}
    ).json()
    return (
        str((body.get("transcode") or {}).get("status") or ""),
        str((body.get("upload") or {}).get("status") or ""),
    )


def wait_for_transcode(
    video_ref: str,
    *,
    timeout: float | None = None,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> None:
    """Block until Vimeo finishes transcoding ``video_ref``.

    Deleting the Zoom source is gated on this, so "complete" has to mean Vimeo
    can actually serve the video — not merely that the bytes arrived.

    Raises VimeoError on a failed transcode or on timeout.
    """
    deadline_budget = timeout if timeout is not None else float(settings.video_transcode_timeout_seconds)
    waited = 0.0
    while True:
        transcode_status, upload_status = get_transcode_status(video_ref)
        if transcode_status == "complete":
            logger.info("Vimeo transcode complete — video=%s", video_ref)
            return
        if transcode_status == "error" or upload_status == "error":
            raise VimeoError(
                f"Vimeo transcode failed for {video_ref} "
                f"(transcode={transcode_status or 'unknown'}, upload={upload_status or 'unknown'})"
            )
        if waited >= deadline_budget:
            raise VimeoError(
                f"Vimeo transcode did not finish within {deadline_budget:.0f}s "
                f"for {video_ref} (last status: {transcode_status or 'unknown'})"
            )
        sleep(poll_interval)
        waited += poll_interval


def set_thumbnail(video_ref: str, image: bytes) -> str:
    """Make ``image`` the video's poster frame, and return the picture's uri.

    Three calls, because Vimeo models a thumbnail as a resource that exists
    before it has content: create the picture record, PUT the bytes to the
    pre-signed link it hands back, then activate it. Skipping the activate
    leaves the upload attached to the video but never shown, which looks exactly
    like the upload having failed.

    Raises:
        VimeoError: at any of the three steps. The caller decides whether that
            is fatal — for the replay pipeline it is not, because a poster frame
            is cosmetic and the video is already published by then.
    """
    if not image:
        raise VimeoError("Refusing to set an empty thumbnail")

    created = _request("POST", f"/videos/{video_ref}/pictures").json()
    picture_uri = created.get("uri") or ""
    upload_link = created.get("link") or ""
    if not picture_uri or not upload_link:
        raise VimeoError(f"Vimeo returned no picture upload link for {video_ref}")

    # Pre-signed link — sending the Authorization header would be rejected.
    try:
        response = httpx.put(upload_link, content=image, timeout=_UPLOAD_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise VimeoError(f"Could not upload the thumbnail for {video_ref}: {exc}") from exc

    _request("PATCH", picture_uri, json={"active": True})
    logger.info("Vimeo thumbnail set — video=%s bytes=%d", video_ref, len(image))
    return picture_uri
