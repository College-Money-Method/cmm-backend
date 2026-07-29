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
