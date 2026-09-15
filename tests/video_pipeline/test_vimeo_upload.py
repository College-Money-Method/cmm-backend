"""tus upload loop, embed-only publishing, and the transcode wait.

The two behaviours worth pinning are the ones with no visible failure mode:
a tus loop that trusts a local counter instead of the server's ``Upload-Offset``
silently truncates the video, and an embed-only upload whose whitelist was never
populated looks published while playing nowhere.
"""

from __future__ import annotations

import pytest

from src.integrations import vimeo_upload
from src.integrations.vimeo import VimeoError


class _Response:
    """Minimal stand-in for the httpx response `_request` returns."""

    def __init__(self, body=None, headers=None):
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


# ── embed domains ────────────────────────────────────────────────────────────


def test_embed_domains_splits_and_trims_the_configured_list(monkeypatch):
    monkeypatch.setattr(
        vimeo_upload.settings,
        "vimeo_embed_domains",
        " collegemoneymethod.com , localhost:5173 ,, ",
    )

    assert vimeo_upload.embed_domains() == ["collegemoneymethod.com", "localhost:5173"]


def test_privacy_is_embed_only_not_unlisted():
    """`unlisted` would still be playable by anyone holding the link."""
    assert vimeo_upload.EMBED_ONLY_PRIVACY == {"view": "disable", "embed": "whitelist"}


# ── tus upload loop ──────────────────────────────────────────────────────────


def test_upload_follows_the_server_reported_offset(tmp_path, monkeypatch):
    """The server is the authority on how much it actually kept."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 300)
    monkeypatch.setattr(vimeo_upload, "_CHUNK_BYTES", 100)

    sent_offsets: list[int] = []
    # Second PATCH is short-written: only 60 of the 100 bytes stuck.
    replies = iter([100, 160, 260, 300])

    def fake_patch(url, *, content, headers, timeout):
        sent_offsets.append(int(headers["Upload-Offset"]))
        return _Response(headers={"Upload-Offset": str(next(replies))})

    monkeypatch.setattr(vimeo_upload.httpx, "patch", fake_patch)

    vimeo_upload._upload_bytes("https://tus.example/abc", source, 300)

    # Every chunk resumes from what the server confirmed, not from a local sum.
    assert sent_offsets == [0, 100, 160, 260]


def test_upload_stops_when_the_server_stops_making_progress(tmp_path, monkeypatch):
    """Without this the loop spins forever against a stuck endpoint."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 200)
    monkeypatch.setattr(vimeo_upload, "_CHUNK_BYTES", 100)
    monkeypatch.setattr(
        vimeo_upload.httpx,
        "patch",
        lambda url, **kw: _Response(headers={"Upload-Offset": "0"}),
    )

    with pytest.raises(VimeoError, match="made no progress"):
        vimeo_upload._upload_bytes("https://tus.example/abc", source, 200)


def test_empty_file_is_refused_before_a_video_record_is_created(tmp_path, monkeypatch):
    """Creating the record first would leave an empty placeholder on the account."""
    empty = tmp_path / "trimmed.mp4"
    empty.write_bytes(b"")
    monkeypatch.setattr(
        vimeo_upload,
        "_request",
        lambda *a, **kw: pytest.fail("no Vimeo call should happen for an empty file"),
    )

    with pytest.raises(VimeoError, match="empty file"):
        vimeo_upload.create_video(empty, "Webinar")


# ── create_video ─────────────────────────────────────────────────────────────


def _stub_upload(monkeypatch, *, player_embed_url, domain_calls):
    """Wire `create_video` up to a fake API that records the domain PUTs.

    The upload path is pinned to the token owner's own library. It is otherwise
    read from settings, so a developer with a team account configured in their
    dotenv would have these tests assert against `/users/<id>/videos` — a
    difference in their environment, not in the code under test.
    """
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "")

    def fake_request(method, path, **kwargs):
        if method == "POST" and path == "/me/videos":
            return _Response(
                {
                    "uri": "/videos/987654321",
                    "player_embed_url": player_embed_url,
                    "upload": {"upload_link": "https://tus.example/abc"},
                }
            )
        if method == "PUT" and "/privacy/domains/" in path:
            domain_calls.append(path.rsplit("/", 1)[-1])
            return _Response({})
        raise AssertionError(f"unexpected Vimeo call: {method} {path}")

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)
    monkeypatch.setattr(vimeo_upload, "_upload_bytes", lambda *a, **kw: None)


def test_create_video_whitelists_every_domain_before_returning(tmp_path, monkeypatch):
    """A whitelist with zero domains blocks the player everywhere, prod included."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(
        vimeo_upload.settings, "vimeo_embed_domains", "collegemoneymethod.com,localhost:5173"
    )
    domains: list[str] = []
    _stub_upload(
        monkeypatch,
        player_embed_url="https://player.vimeo.com/video/987654321?h=deadbeef01",
        domain_calls=domains,
    )

    result = vimeo_upload.create_video(source, "Paying for College")

    assert domains == ["collegemoneymethod.com", "localhost:5173"]
    assert result["video_id"] == "987654321"
    assert result["hash"] == "deadbeef01"
    # The ref carries the hash so later chapter/privacy calls address the video
    # the same way the player does.
    assert result["video_ref"] == "987654321:deadbeef01"


def test_create_video_without_a_privacy_hash_uses_the_bare_id(tmp_path, monkeypatch):
    """Whether an embed-only video gets a hash is Vimeo's call, not ours."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_embed_domains", "collegemoneymethod.com")
    _stub_upload(
        monkeypatch,
        player_embed_url="https://player.vimeo.com/video/987654321",
        domain_calls=[],
    )

    result = vimeo_upload.create_video(source, "Paying for College")

    assert result["hash"] == ""
    assert result["video_ref"] == "987654321"


def test_create_video_raises_when_a_domain_cannot_be_registered(tmp_path, monkeypatch):
    """Failing loudly beats returning a video that looks published and plays nowhere."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_embed_domains", "collegemoneymethod.com")

    def fake_request(method, path, **kwargs):
        if method == "POST":
            return _Response(
                {"uri": "/videos/1", "player_embed_url": "", "upload": {"upload_link": "u"}}
            )
        raise VimeoError("403 Forbidden")

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)
    monkeypatch.setattr(vimeo_upload, "_upload_bytes", lambda *a, **kw: None)

    with pytest.raises(VimeoError):
        vimeo_upload.create_video(source, "Paying for College")


# ── transcode wait ───────────────────────────────────────────────────────────


def test_wait_returns_once_transcode_completes(monkeypatch):
    statuses = iter([("in_progress", "complete"), ("complete", "complete")])
    monkeypatch.setattr(vimeo_upload, "get_transcode_status", lambda ref: next(statuses))
    slept: list[float] = []

    vimeo_upload.wait_for_transcode("1", timeout=100, poll_interval=5, sleep=slept.append)

    assert slept == [5]


def test_wait_raises_on_a_failed_transcode(monkeypatch):
    """Deleting the Zoom copy is gated on this — it must not report success."""
    monkeypatch.setattr(vimeo_upload, "get_transcode_status", lambda ref: ("error", "complete"))

    with pytest.raises(VimeoError, match="transcode failed"):
        vimeo_upload.wait_for_transcode("1", timeout=100, poll_interval=5, sleep=lambda s: None)


def test_wait_raises_on_a_failed_upload_even_if_transcode_looks_pending(monkeypatch):
    monkeypatch.setattr(vimeo_upload, "get_transcode_status", lambda ref: ("in_progress", "error"))

    with pytest.raises(VimeoError, match="transcode failed"):
        vimeo_upload.wait_for_transcode("1", timeout=100, poll_interval=5, sleep=lambda s: None)


def test_wait_times_out_after_the_budget_rather_than_polling_forever(monkeypatch):
    monkeypatch.setattr(vimeo_upload, "get_transcode_status", lambda ref: ("in_progress", "complete"))
    slept: list[float] = []

    with pytest.raises(VimeoError, match="did not finish within"):
        vimeo_upload.wait_for_transcode("1", timeout=30, poll_interval=10, sleep=slept.append)

    assert slept == [10, 10, 10]


def test_wait_falls_back_to_the_configured_timeout(monkeypatch):
    monkeypatch.setattr(vimeo_upload.settings, "video_transcode_timeout_seconds", 20)
    monkeypatch.setattr(vimeo_upload, "get_transcode_status", lambda ref: ("in_progress", ""))
    slept: list[float] = []

    with pytest.raises(VimeoError, match="within 20s"):
        vimeo_upload.wait_for_transcode("1", poll_interval=10, sleep=slept.append)

    assert slept == [10, 10]


# ── where a video is created ─────────────────────────────────────────────────


def test_the_video_is_created_in_the_token_owners_library_by_default(monkeypatch):
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "")

    assert vimeo_upload.upload_owner_path() == "/me/videos"


def test_a_configured_owner_is_uploaded_to_directly(monkeypatch):
    """The team account owns the library the site embeds from, so a replay has
    to be created there rather than created privately and moved."""
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "/users/151255816/")

    assert vimeo_upload.upload_owner_path() == "/users/151255816/videos"


def test_the_folder_is_set_when_the_video_is_created(tmp_path, monkeypatch):
    """Moving a video into a folder afterwards is a separately granted
    permission; a create that succeeds and a move that fails leaves the video
    loose in the library, which is the one outcome an audit run must avoid."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_embed_domains", "collegemoneymethod.com")
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "")
    bodies: list[dict] = []

    def fake_request(method, path, **kwargs):
        if method == "POST":
            bodies.append(kwargs["json"])
            return _Response(
                {"uri": "/videos/1", "player_embed_url": "", "upload": {"upload_link": "u"}}
            )
        return _Response({})

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)
    monkeypatch.setattr(vimeo_upload, "_upload_bytes", lambda *a, **kw: None)

    vimeo_upload.create_video(source, "Audit", folder_uri="/users/151255816/projects/30467578")

    assert bodies[0]["folder_uri"] == "/users/151255816/projects/30467578"
    # Privacy is unchanged by filing it away: an audit copy of a school's
    # session is no less sensitive than a published one.
    assert bodies[0]["privacy"] == vimeo_upload.EMBED_ONLY_PRIVACY


def test_no_folder_key_is_sent_when_there_is_no_folder(tmp_path, monkeypatch):
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_embed_domains", "collegemoneymethod.com")
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "")
    bodies: list[dict] = []

    def fake_request(method, path, **kwargs):
        if method == "POST":
            bodies.append(kwargs["json"])
            return _Response(
                {"uri": "/videos/1", "player_embed_url": "", "upload": {"upload_link": "u"}}
            )
        return _Response({})

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)
    monkeypatch.setattr(vimeo_upload, "_upload_bytes", lambda *a, **kw: None)

    vimeo_upload.create_video(source, "Replay")

    assert "folder_uri" not in bodies[0]


def test_a_refused_cross_account_create_says_what_the_token_needs(tmp_path, monkeypatch):
    """Vimeo scopes upload permission to the API app, not the token, so "403"
    alone sends whoever reads it looking at the wrong thing."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "/users/151255816")

    def fake_request(method, path, **kwargs):
        raise VimeoError("This app can only upload to the app owner's account", 403)

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)

    with pytest.raises(VimeoError, match="OAuth-authorised"):
        vimeo_upload.create_video(source, "Audit")


def test_a_create_that_never_reached_vimeo_is_not_blamed_on_the_token(tmp_path, monkeypatch):
    """A transport failure carries no status, and dressing it up as a
    permissions problem is expensive: it sends whoever reads it to rotate a
    credential while the request is failing before it leaves the machine."""
    source = tmp_path / "trimmed.mp4"
    source.write_bytes(b"x" * 10)
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", "/users/151255816")

    def fake_request(method, path, **kwargs):
        raise VimeoError("Could not reach Vimeo: [Errno -2] Name or service not known")

    monkeypatch.setattr(vimeo_upload, "_request", fake_request)

    with pytest.raises(VimeoError) as caught:
        vimeo_upload.create_video(source, "Audit")

    assert "Name or service not known" in str(caught.value)
    assert "VIMEO_ACCESS_TOKEN" not in str(caught.value)


# ── configured URIs ──────────────────────────────────────────────────────────


def test_a_quoted_owner_uri_is_unquoted_rather_than_put_into_the_url(monkeypatch):
    """These values live in dotenv files, where they are written quoted. A
    loader that hands the quotes through puts one inside the request URL, which
    moves the hostname to `api.vimeo.com"` — so the symptom is a DNS failure
    that names nothing about the configuration that caused it.
    """
    monkeypatch.setattr(vimeo_upload.settings, "vimeo_upload_user_uri", '"/users/151255816"')

    assert vimeo_upload.upload_owner_path() == "/users/151255816/videos"


def test_a_quoted_audit_folder_uri_is_unquoted_too(monkeypatch):
    monkeypatch.setattr(vimeo_upload.operator_settings, "vimeo_audit_folder_uri", lambda: "")
    monkeypatch.setattr(
        vimeo_upload.settings,
        "vimeo_audit_folder_uri",
        '"/users/151255816/projects/30467578"',
    )

    assert vimeo_upload.audit_folder_uri() == "/users/151255816/projects/30467578"


# ── thumbnails ───────────────────────────────────────────────────────────────


def test_setting_a_thumbnail_creates_uploads_and_activates(monkeypatch):
    """Vimeo models a thumbnail as a resource that exists before it has content.
    Skipping the activate leaves the image attached but never shown, which looks
    exactly like the upload having failed."""
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return _Response({"uri": "/videos/9/pictures/7", "link": "https://upload.example/pic"})
        return _Response({})

    put: dict = {}

    def fake_put(url, content=None, timeout=None):
        put["url"], put["content"] = url, content
        return _Response()

    monkeypatch.setattr(vimeo_upload, "_request", request)
    monkeypatch.setattr(vimeo_upload.httpx, "put", fake_put)

    uri = vimeo_upload.set_thumbnail("9:hash", b"jpegbytes")

    assert uri == "/videos/9/pictures/7"
    assert calls == [("POST", "/videos/9:hash/pictures"), ("PATCH", "/videos/9/pictures/7")]
    assert put == {"url": "https://upload.example/pic", "content": b"jpegbytes"}


def test_a_thumbnail_with_no_upload_link_is_an_error_not_a_silent_skip(monkeypatch):
    monkeypatch.setattr(vimeo_upload, "_request", lambda *a, **k: _Response({"uri": "/x"}))

    with pytest.raises(VimeoError, match="no picture upload link"):
        vimeo_upload.set_thumbnail("9", b"jpegbytes")


def test_an_empty_image_is_refused_before_any_call_is_made():
    with pytest.raises(VimeoError, match="empty thumbnail"):
        vimeo_upload.set_thumbnail("9", b"")
