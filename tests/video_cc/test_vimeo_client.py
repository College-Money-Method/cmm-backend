"""Unit tests for Vimeo video-reference parsing and language resolution.

No network: the language list is stubbed. What is covered is the mapping logic
that decides which Vimeo code a caption track is created under — getting "zh"
wrong publishes Mandarin captions as Traditional Chinese.
"""

import pytest

from src.integrations import vimeo
from src.integrations.vimeo import VimeoError, extract_video_ref


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://vimeo.com/123456789", "123456789"),
        ("https://vimeo.com/123456789/abc123def", "123456789:abc123def"),
        ("http://vimeo.com/987", "987"),
        ("https://player.vimeo.com/video/555", "555"),
        (
            '<iframe src="https://player.vimeo.com/video/76543210?h=deadbeef01&badge=0" '
            'width="640" height="360" frameborder="0"></iframe>',
            "76543210:deadbeef01",
        ),
        ("  424242  ", "424242"),
        ("424242:hash99", "424242:hash99"),
    ],
)
def test_extracts_video_reference(raw, expected):
    assert extract_video_ref(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "https://youtube.com/watch?v=abc", "not a url"])
def test_rejects_unusable_input(raw):
    with pytest.raises(VimeoError):
        extract_video_ref(raw)


@pytest.fixture
def stub_languages(monkeypatch):
    """Pin the Vimeo language list so resolution is deterministic and offline."""
    monkeypatch.setattr(
        vimeo,
        "_fetch_languages",
        lambda: {
            "es": "Spanish",
            "zh": "Chinese",
            "zh-Hans": "Chinese (Simplified)",
            "zh-Hant": "Chinese (Traditional)",
            "pt": "Portuguese",
            "fr": "French",
        },
    )


def test_mandarin_resolves_to_simplified_not_generic_chinese(stub_languages):
    """Vimeo's bare "zh" is generic Chinese — Mandarin must pin to zh-Hans."""
    assert vimeo.resolve_language("zh", "Chinese (Simplified)") == (
        "zh-Hans",
        "Chinese (Simplified)",
    )


def test_traditional_chinese_is_distinct_from_simplified(stub_languages):
    assert vimeo.resolve_language("zh-Hant", "x")[0] == "zh-Hant"


@pytest.mark.parametrize("locale", ["es", "pt", "fr"])
def test_exact_codes_pass_through(locale, stub_languages):
    code, name = vimeo.resolve_language(locale, "fallback")
    assert code == locale
    # Display name comes from Vimeo's own list, not the caller's fallback.
    assert name != "fallback"


def test_unsupported_language_raises(stub_languages):
    with pytest.raises(VimeoError, match="does not offer"):
        vimeo.resolve_language("klingon", "Klingon")


def test_falls_back_to_verbatim_locale_when_list_unavailable(monkeypatch):
    """A transient /languages failure must not abort an otherwise valid job."""

    def boom():
        raise VimeoError("Vimeo API error 503")

    monkeypatch.setattr(vimeo, "_fetch_languages", boom)
    assert vimeo.resolve_language("es", "Spanish") == ("es", "Spanish")


# ── Source-track download ─────────────────────────────────────────────────────

TRACKS = [
    {"uri": "/v/1/tt/1", "language": "en", "active": False, "link": "https://x/inactive.vtt",
     "name": "old.vtt"},
    {"uri": "/v/1/tt/2", "language": "en", "active": True, "link": "https://x/active.vtt",
     "name": "current.vtt"},
    {"uri": "/v/1/tt/3", "language": "es", "active": True, "link": "https://x/spanish.vtt",
     "name": "es.vtt"},
]


@pytest.fixture
def stub_tracks(monkeypatch):
    """Serve a fixed track list and echo which link was downloaded."""
    monkeypatch.setattr(vimeo, "list_text_tracks", lambda ref, fields=None: TRACKS)

    class Resp:
        def __init__(self, url):
            self.url = url
            self.text = f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nfrom {url}\n"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(vimeo.httpx, "get", lambda url, **kw: Resp(url))


def test_active_track_wins_when_a_video_has_several_in_one_language(stub_tracks):
    """Real videos carry duplicate English tracks; the player shows the active one."""
    content, name = vimeo.download_source_track("1", "en")
    assert "active.vtt" in content
    assert name == "current.vtt"


def test_source_language_filter_is_respected(stub_tracks):
    content, name = vimeo.download_source_track("1", "es")
    assert "spanish.vtt" in content
    assert name == "es.vtt"


def test_missing_source_language_raises_actionable_error(stub_tracks):
    with pytest.raises(VimeoError, match="no fr caption track"):
        vimeo.download_source_track("1", "fr")


def test_non_webvtt_response_is_rejected(monkeypatch, stub_tracks):
    class Html:
        text = "<html>expired link</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(vimeo.httpx, "get", lambda url, **kw: Html())
    with pytest.raises(VimeoError, match="did not come back as WebVTT"):
        vimeo.download_source_track("1", "en")


# ── Rate limiting ─────────────────────────────────────────────────────────────


class FakeResp:
    """Minimal httpx.Response stand-in for rate-limit tests."""

    def __init__(self, status, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def test_retry_after_uses_the_reset_header_not_the_local_clock():
    """Container clock skew must not produce a negative or huge sleep."""
    resp = FakeResp(
        429,
        {
            "x-ratelimit-reset": "2026-07-30T05:37:34+00:00",
            "date": "Thu, 30 Jul 2026 05:37:09 GMT",
        },
    )
    # 25s until reset, +1s so we land past the boundary.
    assert vimeo._retry_after_seconds(resp) == pytest.approx(26.0, abs=0.1)


@pytest.mark.parametrize(
    "headers",
    [
        {},                                            # no reset header at all
        {"x-ratelimit-reset": "not-a-timestamp"},      # unparseable
    ],
)
def test_retry_after_falls_back_when_headers_are_unusable(headers):
    assert vimeo._retry_after_seconds(FakeResp(429, headers)) == vimeo._RATE_LIMIT_FALLBACK_WAIT


def test_retry_after_is_capped_and_never_negative():
    past = FakeResp(429, {
        "x-ratelimit-reset": "2026-07-30T05:00:00+00:00",
        "date": "Thu, 30 Jul 2026 05:37:09 GMT",   # reset already elapsed
    })
    assert vimeo._retry_after_seconds(past) == 1.0

    far = FakeResp(429, {
        "x-ratelimit-reset": "2026-07-30T09:37:34+00:00",  # hours away
        "date": "Thu, 30 Jul 2026 05:37:09 GMT",
    })
    assert far.headers and vimeo._retry_after_seconds(far) == vimeo._RATE_LIMIT_MAX_WAIT


def test_a_rate_limited_request_is_retried_and_succeeds(monkeypatch):
    calls = {"n": 0, "slept": []}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, {
                "x-ratelimit-reset": "2026-07-30T05:37:12+00:00",
                "date": "Thu, 30 Jul 2026 05:37:09 GMT",
            })
        return FakeResp(200, {}, {"name": "ok"})

    monkeypatch.setattr(vimeo.httpx, "request", fake_request)
    monkeypatch.setattr(vimeo.time, "sleep", lambda s: calls["slept"].append(s))
    monkeypatch.setattr(vimeo.settings, "vimeo_access_token", "t")

    assert vimeo._request("GET", "/videos/1").json() == {"name": "ok"}
    assert calls["n"] == 2
    assert calls["slept"] == [4.0]


def test_persistent_rate_limiting_eventually_raises(monkeypatch):
    monkeypatch.setattr(
        vimeo.httpx, "request", lambda *a, **kw: FakeResp(429, {}, {"error": "slow down"})
    )
    monkeypatch.setattr(vimeo.time, "sleep", lambda s: None)
    monkeypatch.setattr(vimeo.settings, "vimeo_access_token", "t")

    with pytest.raises(VimeoError) as exc:
        vimeo._request("GET", "/videos/1")
    assert exc.value.status == 429


def test_scope_check_is_cached_per_token(monkeypatch):
    """/oauth/verify runs first in every job — re-fetching it burns the 50/min budget."""
    calls = {"n": 0}

    def fake_request(method, path, **kw):
        calls["n"] += 1
        return FakeResp(200, {}, {"scope": vimeo.REQUIRED_SCOPES})

    monkeypatch.setattr(vimeo, "_request", fake_request)
    monkeypatch.setattr(vimeo, "_scope_cache", None)
    monkeypatch.setattr(vimeo.settings, "vimeo_access_token", "token-a")

    assert vimeo.missing_scopes() == []
    assert vimeo.missing_scopes() == []
    assert calls["n"] == 1, "second call must come from cache"

    # Rotating the token must re-verify rather than trust the cached answer.
    monkeypatch.setattr(vimeo.settings, "vimeo_access_token", "token-b")
    assert vimeo.missing_scopes() == []
    assert calls["n"] == 2
