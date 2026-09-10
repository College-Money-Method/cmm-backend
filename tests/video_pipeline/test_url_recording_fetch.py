"""Fetching an audit source from a pasted URL.

The task makes this request from inside the VPC with a role attached, so most
of these are about where it will and will not connect: a pasted internal
hostname or either metadata address has to be refused before any bytes move,
and again after each redirect, because a public host that redirects to
169.254.169.254 would otherwise reach the credentials the role holds.
"""

from __future__ import annotations

import httpx
import pytest

from src.video_pipeline import url_recording_fetch
from src.video_pipeline.url_recording_fetch import UrlFetchError, assert_public_url, fetch_from_url

URL = "https://cmm-media.s3.amazonaws.com/raw/session.mp4"


# A globally routable address, deliberately not one of the documentation
# ranges: `ipaddress.is_global` is false for 203.0.113.0/24 and friends, so a
# TEST-NET address here would be refused for the wrong reason and every
# happy-path test below would pass while proving nothing.
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture
def resolves_public(monkeypatch):
    """Every host answers with one public address unless a test says otherwise."""
    monkeypatch.setattr(url_recording_fetch, "_resolved_addresses", lambda host: [PUBLIC_ADDRESS])


def _serve(monkeypatch, handler):
    """Point the module's client at ``handler`` instead of the network."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(url_recording_fetch.httpx, "Client", client_factory)


# ── where it will connect ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.3.14",
        "192.168.1.20",
        "169.254.169.254",  # EC2 instance metadata
        "169.254.170.2",  # ECS task metadata — the task role's credentials
        "::1",
    ],
)
def test_a_host_resolving_to_a_private_address_is_refused(monkeypatch, address):
    monkeypatch.setattr(url_recording_fetch, "_resolved_addresses", lambda host: [address])

    with pytest.raises(UrlFetchError, match="not a public address"):
        assert_public_url("https://internal.example/a.mp4")


def test_a_host_with_one_private_answer_among_public_ones_is_refused(monkeypatch):
    """Allowing it would make each connection a coin flip."""
    monkeypatch.setattr(
        url_recording_fetch, "_resolved_addresses", lambda host: [PUBLIC_ADDRESS, "10.0.0.5"]
    )

    with pytest.raises(UrlFetchError, match="not a public address"):
        assert_public_url(URL)


def test_a_public_host_is_allowed(resolves_public):
    assert_public_url(URL) is None


def test_http_cannot_be_fetched(resolves_public):
    with pytest.raises(UrlFetchError, match="Only https"):
        assert_public_url("http://example.com/a.mp4")


def test_a_url_with_no_host_is_refused(resolves_public):
    with pytest.raises(UrlFetchError, match="no host"):
        assert_public_url("https:///a.mp4")


def test_a_name_that_does_not_resolve_says_so(monkeypatch):
    import socket

    def explode(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(url_recording_fetch.socket, "getaddrinfo", explode)

    with pytest.raises(UrlFetchError, match="does not resolve"):
        assert_public_url("https://nope.invalid/a.mp4")


# ── the download ─────────────────────────────────────────────────────────────


def test_the_file_is_streamed_to_disk(monkeypatch, resolves_public, tmp_path):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"mp4-bytes" * 100))

    written = fetch_from_url(URL, tmp_path / "nested" / "source.mp4")

    assert written.read_bytes() == b"mp4-bytes" * 100


def test_an_error_status_is_reported_rather_than_written(monkeypatch, resolves_public, tmp_path):
    _serve(monkeypatch, lambda request: httpx.Response(403, text="expired"))
    dest = tmp_path / "source.mp4"

    with pytest.raises(UrlFetchError, match="failed"):
        fetch_from_url(URL, dest)


def test_a_web_page_is_refused_before_ffmpeg_sees_it(monkeypatch, resolves_public, tmp_path):
    """A share page pasted instead of a download link reaches ffmpeg as "moov
    atom not found", which tells the operator nothing about the real mistake."""
    _serve(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"),
    )

    with pytest.raises(UrlFetchError, match="looks like a web page"):
        fetch_from_url(URL, tmp_path / "source.mp4")


def test_an_empty_body_is_refused(monkeypatch, resolves_public, tmp_path):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b""))

    with pytest.raises(UrlFetchError, match="empty file"):
        fetch_from_url(URL, tmp_path / "source.mp4")


def test_a_source_over_the_size_limit_is_abandoned(monkeypatch, resolves_public, tmp_path):
    monkeypatch.setattr(url_recording_fetch, "_MAX_BYTES", 16)
    monkeypatch.setattr(url_recording_fetch, "_STREAM_CHUNK", 8)
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 64))

    with pytest.raises(UrlFetchError, match="exceeds"):
        fetch_from_url(URL, tmp_path / "source.mp4")


# ── redirects ────────────────────────────────────────────────────────────────


def test_a_redirect_to_a_public_host_is_followed(monkeypatch, resolves_public, tmp_path):
    def handler(request):
        if request.url.path == "/raw/session.mp4":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/final.mp4"})
        return httpx.Response(200, content=b"redirected-bytes")

    _serve(monkeypatch, handler)

    assert fetch_from_url(URL, tmp_path / "s.mp4").read_bytes() == b"redirected-bytes"


def test_a_redirect_into_the_metadata_endpoint_is_refused(monkeypatch, tmp_path):
    """This is the case httpx's own redirect handling would have followed."""
    hosts = {"cmm-media.s3.amazonaws.com": [PUBLIC_ADDRESS], "169.254.169.254": ["169.254.169.254"]}
    monkeypatch.setattr(url_recording_fetch, "_resolved_addresses", lambda host: hosts[host])
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://169.254.169.254/latest/meta-data/iam/"}
        ),
    )

    with pytest.raises(UrlFetchError, match="not a public address"):
        fetch_from_url(URL, tmp_path / "s.mp4")


def test_a_redirect_loop_ends(monkeypatch, resolves_public, tmp_path):
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": str(request.url)}))

    with pytest.raises(UrlFetchError, match="redirected more than"):
        fetch_from_url(URL, tmp_path / "s.mp4")
