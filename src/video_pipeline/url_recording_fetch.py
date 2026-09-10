"""Download an audit run's source from a pasted https URL.

The Zoom path re-derives its own download URL from S2S OAuth; this one is
handed a URL by an operator, which is a different risk. The task runs in the
VPC with a role attached, so a URL is a request this process makes from inside
the network — the destination is checked against the address it actually
resolves to before any bytes move, and again on every redirect hop, which is
why redirects are followed by hand here rather than by httpx.

That check stops the accidents and the casual cases: a copy-pasted internal
hostname, ``localhost``, or either metadata endpoint (169.254.169.254 and the
ECS task's 169.254.170.2, both link-local) where the task role's credentials
live. It is not a hardened defence — the name is resolved once for the check
and again by the connection, so someone controlling DNS for the host could
still swap the answer between the two. The paste comes from a super_admin, so
that is the proportionate depth; do not reuse this for a URL an ordinary user
can supply without closing that gap first.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=300.0)
_STREAM_CHUNK = 8 * 1024 * 1024
_MAX_REDIRECTS = 5

# The task's ephemeral disk also holds the trimmed copy and every sampled
# frame. A pasted URL has no size Zoom's account limits would bound, so refuse
# a download that could fill the volume rather than dying part-way through with
# nothing to show for it.
_MAX_BYTES = 8 * 1024 * 1024 * 1024

# A pasted page URL would otherwise reach ffmpeg as an unreadable file, and
# "moov atom not found" is a poor way to learn you copied the wrong link.
_REJECTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class UrlFetchError(RuntimeError):
    """The URL was refused, or the download failed."""


def _resolved_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlFetchError(f"'{host}' does not resolve: {exc}") from exc
    return [info[4][0] for info in infos]


def assert_public_url(url: str) -> None:
    """Raise :class:`UrlFetchError` unless ``url`` is https on a public address.

    Every address the host resolves to has to be public: a name with one public
    and one private answer would otherwise be a coin flip per connection.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UrlFetchError(f"Only https URLs can be fetched (got '{parsed.scheme}')")
    host = parsed.hostname
    if not host:
        raise UrlFetchError(f"'{url}' has no host")

    for address in _resolved_addresses(host):
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast:
            raise UrlFetchError(
                f"Refusing to fetch '{host}' — it resolves to {ip}, which is not a "
                "public address. Only internet-reachable sources can be audited."
            )


def _check_content_type(resp: httpx.Response, url: str) -> None:
    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in _REJECTED_CONTENT_TYPES:
        raise UrlFetchError(
            f"'{url}' served {content_type}, not a video file. It looks like a web page "
            "rather than a direct download link."
        )


def _open_stream(client: httpx.Client, url: str):
    """Follow redirects by hand, validating each hop, and return the open stream.

    The context manager is returned still open: the caller streams from it.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        assert_public_url(url)
        stream = client.stream("GET", url)
        resp = stream.__enter__()
        location = resp.headers.get("location")
        if resp.is_redirect and location:
            resp.read()
            stream.__exit__(None, None, None)
            url = str(resp.url.join(location))
            logger.info("Source URL redirected to %s", url)
            continue
        try:
            resp.raise_for_status()
            _check_content_type(resp, url)
        except Exception:
            stream.__exit__(None, None, None)
            raise
        return stream, resp

    raise UrlFetchError(f"Source URL redirected more than {_MAX_REDIRECTS} times")


def fetch_from_url(url: str, dest: Path) -> Path:
    """Stream ``url`` to ``dest``. Returns the path written.

    Streamed for the same reason the Zoom download is: these are 1-2 GB files
    and the task needs its memory for ffmpeg.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
            stream, resp = _open_stream(client, url)
            try:
                with dest.open("wb") as handle:
                    for chunk in resp.iter_bytes(_STREAM_CHUNK):
                        written += len(chunk)
                        if written > _MAX_BYTES:
                            raise UrlFetchError(
                                f"Source exceeds the {_MAX_BYTES // (1024**3)} GB limit — "
                                "trim it before uploading, or use a Zoom recording"
                            )
                        handle.write(chunk)
            finally:
                stream.__exit__(None, None, None)
    except httpx.HTTPError as exc:
        raise UrlFetchError(f"Downloading {url} failed: {exc}") from exc

    if written == 0:
        raise UrlFetchError(f"'{url}' returned an empty file")
    logger.info("Downloaded audit source — %d bytes from %s", written, url)
    return dest
