"""Unit tests for src/analytics/posthog.py — cache and query helpers."""

import uuid
import datetime
from datetime import timezone
from unittest.mock import MagicMock, patch

import pytest

from src.analytics import posthog as ph
from src.analytics.schemas import TrendMetric, FunnelStep, TopBreakdown


# ── _key ──────────────────────────────────────────────────────────────────────

def test_key_with_string_school_id():
    k = ph._key(fn="trend", school_id="abc-123", event="$pageview")
    assert isinstance(k, str) and len(k) == 32


def test_key_with_uuid_school_id():
    """UUID must not raise TypeError (the original bug)."""
    uid = uuid.uuid4()
    k = ph._key(fn="trend", school_id=uid, event="$pageview")
    assert isinstance(k, str) and len(k) == 32


def test_key_is_deterministic():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    k1 = ph._key(fn="trend", school_id=uid, event="$pageview")
    k2 = ph._key(fn="trend", school_id=uid, event="$pageview")
    assert k1 == k2


def test_key_differs_on_different_inputs():
    k1 = ph._key(fn="trend", event="$pageview", school_id=None)
    k2 = ph._key(fn="trend", event="user_signed_in", school_id=None)
    assert k1 != k2


# ── Cache helpers ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_cache():
    """Isolate each test by clearing the module-level cache dict."""
    ph._cache.clear()
    yield
    ph._cache.clear()


def test_cache_miss_on_empty():
    assert ph._get("nonexistent") is None


def test_cache_hit_within_ttl():
    ph._set("k1", {"result": 42})
    assert ph._get("k1") == {"result": 42}


def test_cache_expiry():
    """Stale entries (beyond TTL) should be evicted and return None."""
    ph._cache["k2"] = ({"result": "old"}, datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=31))
    assert ph._get("k2") is None
    assert "k2" not in ph._cache  # evicted


def test_cache_not_expired_at_boundary():
    """Entries just within TTL (29 min old) should still be returned."""
    ph._cache["k3"] = ({"result": "fresh"}, datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=29))
    assert ph._get("k3") == {"result": "fresh"}


# ── _school_filter ────────────────────────────────────────────────────────────

def test_school_filter_with_id():
    f = ph._school_filter("school-uuid")
    assert f == [{"key": "school_id", "value": "school-uuid", "operator": "exact", "type": "event"}]


def test_school_filter_none():
    assert ph._school_filter(None) == []


def test_school_filter_person_type():
    f = ph._school_filter("sid", prop_type="person")
    assert f[0]["type"] == "person"


# ── get_trend ─────────────────────────────────────────────────────────────────

TREND_RESPONSE = {
    "results": [{"count": 42, "data": [5.0, 10.0, 27.0], "days": ["2026-06-09", "2026-06-10", "2026-06-11"]}]
}


def test_get_trend_returns_metric():
    with patch("src.analytics.posthog._query", return_value=TREND_RESPONSE):
        m = ph.get_trend("key", "proj", "workshop_watch_recording")
    assert isinstance(m, TrendMetric)
    assert m.total == 42
    assert m.data == [5.0, 10.0, 27.0]
    assert m.days == ["2026-06-09", "2026-06-10", "2026-06-11"]


def test_get_trend_uses_cache_on_second_call():
    with patch("src.analytics.posthog._query", return_value=TREND_RESPONSE) as mock_query:
        ph.get_trend("key", "proj", "workshop_watch_recording")
        ph.get_trend("key", "proj", "workshop_watch_recording")
    assert mock_query.call_count == 1  # second call hit cache


def test_get_trend_includes_school_filter():
    captured: dict = {}

    def fake_query(api_key, project_id, query):
        captured["query"] = query
        return TREND_RESPONSE

    with patch("src.analytics.posthog._query", side_effect=fake_query):
        ph.get_trend("key", "proj", "resource_card_click", school_id="school-xyz")

    props = captured["query"]["properties"]
    assert len(props) == 1 and props[0]["value"] == "school-xyz"


def test_get_trend_no_filter_when_school_none():
    captured: dict = {}

    def fake_query(api_key, project_id, query):
        captured["query"] = query
        return TREND_RESPONSE

    with patch("src.analytics.posthog._query", side_effect=fake_query):
        ph.get_trend("key", "proj", "$pageview", school_id=None)

    assert captured["query"]["properties"] == []


# ── get_funnel ────────────────────────────────────────────────────────────────

FUNNEL_RESPONSE = {
    "result": [[
        {"name": "workshop_register_open", "count": 100},
        {"name": "workshop_registration_complete", "count": 60},
    ]]
}


def test_get_funnel_returns_steps():
    with patch("src.analytics.posthog._query", return_value=FUNNEL_RESPONSE):
        steps = ph.get_funnel("key", "proj", "workshop_register_open", "workshop_registration_complete")
    assert len(steps) == 2
    assert all(isinstance(s, FunnelStep) for s in steps)
    assert steps[0].count == 100
    assert steps[1].count == 60


def test_get_funnel_empty_result():
    with patch("src.analytics.posthog._query", return_value={"result": [[]]}):
        steps = ph.get_funnel("key", "proj", "a", "b")
    assert steps == []


# ── get_top_breakdown ─────────────────────────────────────────────────────────

BREAKDOWN_RESPONSE = {
    "results": [
        {"label": "college aid", "count": 50},
        {"label": "FAFSA", "count": 30},
        {"label": "Other", "count": 200},  # "Other" should be filtered out
    ]
}


def test_get_top_breakdown_excludes_other():
    with patch("src.analytics.posthog._query", return_value=BREAKDOWN_RESPONSE):
        rows = ph.get_top_breakdown("key", "proj", "search_query", "query")
    labels = [r.label for r in rows]
    assert "Other" not in labels
    assert "college aid" in labels


def test_get_top_breakdown_sorted_descending():
    with patch("src.analytics.posthog._query", return_value=BREAKDOWN_RESPONSE):
        rows = ph.get_top_breakdown("key", "proj", "search_query", "query")
    assert rows[0].count >= rows[-1].count


def test_get_top_breakdown_respects_limit():
    many = {"results": [{"label": f"term{i}", "count": i} for i in range(20)]}
    with patch("src.analytics.posthog._query", return_value=many):
        rows = ph.get_top_breakdown("key", "proj", "search_query", "query", limit=5)
    assert len(rows) <= 5
