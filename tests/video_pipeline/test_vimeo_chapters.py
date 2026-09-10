"""Replacing a video's chapter list, and the read-back that proves it happened.

Two failure modes are covered because neither is visible from the API's own
response: a partly-applied list, which leaves a published replay whose menu
disagrees with the job row, and a fallback that fires on the wrong error and
deletes the chapters it was meant to write.
"""

from __future__ import annotations

import pytest

from src.integrations import vimeo_chapters
from src.integrations.vimeo import VimeoError

REF = "987654321:deadbeef01"

CHAPTERS = [
    {"timecode": 0, "title": "Introduction", "source": "intro", "confidence": ""},
    {"timecode": 300, "title": "The Aid Formula", "source": "title_card", "confidence": "match"},
]


class _Response:
    def __init__(self, body=None):
        self._body = body or {}

    def json(self):
        return self._body


def _existing(*timecodes):
    return {
        "data": [
            {"uri": f"/videos/987654321/chapters/{t}", "timecode": t, "title": f"old {t}"}
            for t in timecodes
        ]
    }


def _fake_api(monkeypatch, handler):
    calls: list[tuple[str, str, object]] = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return handler(method, path, kwargs)

    monkeypatch.setattr(vimeo_chapters, "_request", request)
    return calls


# ── payload shaping ──────────────────────────────────────────────────────────


def test_payload_sends_only_what_vimeo_accepts():
    """`source` and `confidence` are ours; Vimeo rejects unknown fields."""
    assert vimeo_chapters._payload(CHAPTERS) == [
        {"timecode": 0, "title": "Introduction"},
        {"timecode": 300, "title": "The Aid Formula"},
    ]


def test_payload_is_ordered_by_timecode():
    out = vimeo_chapters._payload([{"timecode": 90, "title": "b"}, {"timecode": 5, "title": "a"}])

    assert [c["timecode"] for c in out] == [5, 90]


def test_a_duplicate_timecode_is_dropped_rather_than_failing_the_batch():
    out = vimeo_chapters._payload(
        [{"timecode": 7, "title": "first"}, {"timecode": 7, "title": "second"}]
    )

    assert out == [{"timecode": 7, "title": "first"}]


def test_an_over_long_title_is_truncated_not_rejected():
    """Vimeo 400s a title past its limit, taking the whole publish with it."""
    out = vimeo_chapters._payload([{"timecode": 0, "title": "x" * 250}])

    assert len(out[0]["title"]) == vimeo_chapters.MAX_TITLE_CHARS


def test_truncation_stops_at_a_word_rather_than_mid_word():
    """A real title card that failed a live publish, at 59 characters."""
    out = vimeo_chapters._payload(
        [{"timecode": 0, "title": "Navigating the New System of College Pricing & Financial Aid"}]
    )

    assert out[0]["title"] == "Navigating the New System of College Pricing"


def test_a_title_that_already_fits_is_left_alone():
    out = vimeo_chapters._payload([{"timecode": 0, "title": "Understanding need-based aid"}])

    assert out[0]["title"] == "Understanding need-based aid"


def test_a_title_with_no_usable_word_break_is_cut_anyway():
    """The only break is so early that honouring it would throw the title away.
    Better an ugly chapter than a rejected one."""
    out = vimeo_chapters._payload([{"timecode": 0, "title": "A " + "x" * 80}])

    assert len(out[0]["title"]) == vimeo_chapters.MAX_TITLE_CHARS


def test_publishing_no_chapters_is_refused(monkeypatch):
    _fake_api(monkeypatch, lambda *a: pytest.fail("no call should be made"))

    with pytest.raises(VimeoError, match="no chapters"):
        vimeo_chapters.set_chapters(REF, [])


# ── the batch path ───────────────────────────────────────────────────────────


def test_the_batch_endpoint_replaces_the_list_in_one_call(monkeypatch):
    def handler(method, path, kwargs):
        if method == "PUT":
            return _Response({})
        return _Response(_existing(0, 300))

    calls = _fake_api(monkeypatch, handler)

    confirmed = vimeo_chapters.set_chapters(REF, CHAPTERS)

    assert calls[0][0] == "PUT"
    assert calls[0][1].endswith("/chapters/batch")
    assert calls[0][2] == [
        {"timecode": 0, "title": "Introduction"},
        {"timecode": 300, "title": "The Aid Formula"},
    ]
    assert [c["timecode"] for c in confirmed] == [0, 300]


def test_a_short_read_back_fails_rather_than_reporting_success(monkeypatch):
    """A half-written menu on a published replay is worse than a failed job."""

    def handler(method, path, kwargs):
        return _Response({}) if method == "PUT" else _Response(_existing(0))

    _fake_api(monkeypatch, handler)

    with pytest.raises(VimeoError, match="kept 1 of 2"):
        vimeo_chapters.set_chapters(REF, CHAPTERS)


# ── the per-chapter fallback ─────────────────────────────────────────────────


def test_a_missing_batch_endpoint_falls_back_to_per_chapter_writes(monkeypatch):
    state = {"replaced": False}

    def handler(method, path, kwargs):
        if method == "PUT":
            raise VimeoError("The specified resource doesn't exist.", status=404)
        if method == "GET":
            return _Response(_existing(0, 300) if state["replaced"] else _existing(42))
        if method == "DELETE":
            return _Response({})
        if method == "POST":
            state["replaced"] = True
            return _Response({})
        raise AssertionError(method)

    calls = _fake_api(monkeypatch, handler)

    vimeo_chapters.set_chapters(REF, CHAPTERS)

    methods = [c[0] for c in calls]
    # Existing chapters are cleared before new ones are written, or the video
    # ends up with both lists.
    assert methods.index("DELETE") < methods.index("POST")
    assert methods.count("POST") == 2
    assert [c[2] for c in calls if c[0] == "POST"] == [
        {"timecode": 0, "title": "Introduction"},
        {"timecode": 300, "title": "The Aid Formula"},
    ]


def test_a_permission_error_is_not_treated_as_a_missing_endpoint(monkeypatch):
    """403 means the token cannot write — deleting the existing chapters on the
    way to failing anyway would destroy a working menu."""

    def handler(method, path, kwargs):
        if method == "PUT":
            raise VimeoError("forbidden", status=403)
        pytest.fail(f"nothing further should run: {method} {path}")

    _fake_api(monkeypatch, handler)

    with pytest.raises(VimeoError, match="forbidden"):
        vimeo_chapters.set_chapters(REF, CHAPTERS)


def test_get_chapters_returns_them_in_timeline_order(monkeypatch):
    _fake_api(monkeypatch, lambda *a: _Response(_existing(300, 0, 90)))

    assert [c["timecode"] for c in vimeo_chapters.get_chapters(REF)] == [0, 90, 300]


def test_a_video_with_no_chapters_reads_back_as_empty(monkeypatch):
    _fake_api(monkeypatch, lambda *a: _Response({"total": 0, "data": None}))

    assert vimeo_chapters.get_chapters(REF) == []
