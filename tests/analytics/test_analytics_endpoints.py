"""Integration tests for /api/v1/analytics/* endpoints via FastAPI TestClient."""

import uuid
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.auth.deps import require_counselor
from src.auth.schemas import CurrentUser
from src.analytics.schemas import TrendMetric, FunnelStep, TopBreakdown
from src.analytics import posthog as ph

# ── Fixtures ──────────────────────────────────────────────────────────────────

SCHOOL_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SCHOOL_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

ADMIN = CurrentUser(user_id=uuid.uuid4(), role="super_admin", school_id=None)
COUNSELOR_A = CurrentUser(user_id=uuid.uuid4(), role="counselor", school_id=SCHOOL_A)

EMPTY_TREND = TrendMetric(total=0, data=[], days=[])
SAMPLE_TREND = TrendMetric(total=42, data=[5.0, 10.0, 27.0], days=["2026-06-09", "2026-06-10", "2026-06-11"])
SAMPLE_FUNNEL = [FunnelStep(name="Opened", count=100), FunnelStep(name="Completed", count=60)]
SAMPLE_QUERIES = [TopBreakdown(label="FAFSA", count=30), TopBreakdown(label="grants", count=20)]


def admin_client() -> TestClient:
    app.dependency_overrides[require_counselor] = lambda: ADMIN
    return TestClient(app)


def counselor_client() -> TestClient:
    app.dependency_overrides[require_counselor] = lambda: COUNSELOR_A
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_overrides():
    yield
    app.dependency_overrides.clear()
    ph._cache.clear()


@pytest.fixture
def mock_posthog_configured(mocker):
    """Patch settings so posthog is treated as configured."""
    mocker.patch.object(__import__("src.config", fromlist=["settings"]).settings, "posthog_api_key", "phx_test_key")
    mocker.patch.object(__import__("src.config", fromlist=["settings"]).settings, "posthog_project_id", "12345")


# ── /overview ─────────────────────────────────────────────────────────────────

def test_overview_returns_503_when_not_configured(mocker):
    from src.config import settings
    mocker.patch.object(settings, "posthog_api_key", "")
    mocker.patch.object(settings, "posthog_project_id", "")
    client = admin_client()
    resp = client.get("/api/v1/analytics/overview")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_overview_admin_all_schools(mock_posthog_configured):
    with patch.multiple("src.analytics.posthog",
                        get_trend=MagicMock(return_value=SAMPLE_TREND)):
        client = admin_client()
        resp = client.get("/api/v1/analytics/overview")

    assert resp.status_code == 200
    body = resp.json()
    assert "dau" in body and "sign_ins" in body
    assert body["dau"]["total"] == 42


def test_overview_admin_with_school_id(mock_posthog_configured):
    """Admin can pass school_id — it should be forwarded to posthog helpers."""
    calls = []

    def spy_trend(api_key, project_id, event, **kwargs):
        calls.append(kwargs.get("school_id"))
        return SAMPLE_TREND

    with patch("src.analytics.posthog.get_trend", side_effect=spy_trend):
        client = admin_client()
        resp = client.get(f"/api/v1/analytics/overview?school_id={SCHOOL_A}")

    assert resp.status_code == 200
    assert all(str(c) == str(SCHOOL_A) for c in calls if c)


def test_overview_counselor_ignores_school_param(mock_posthog_configured):
    """Counselors are always scoped to their own school, never the query param."""
    calls = []

    def spy_trend(api_key, project_id, event, **kwargs):
        calls.append(kwargs.get("school_id"))
        return SAMPLE_TREND

    with patch("src.analytics.posthog.get_trend", side_effect=spy_trend):
        client = counselor_client()
        # Passing school_B in param should be overridden by counselor's school_A
        resp = client.get(f"/api/v1/analytics/overview?school_id={SCHOOL_B}")

    assert resp.status_code == 200
    # All calls used school_A (counselor's own school), not school_B
    for school_id_used in calls:
        if school_id_used:
            assert str(school_id_used) == str(SCHOOL_A)


# ── /workshop ─────────────────────────────────────────────────────────────────

def test_workshop_endpoint_shape(mock_posthog_configured):
    with patch("src.analytics.posthog.get_trend", return_value=SAMPLE_TREND), \
         patch("src.analytics.posthog.get_funnel", return_value=SAMPLE_FUNNEL), \
         patch("src.analytics.posthog.get_top_breakdown", return_value=SAMPLE_QUERIES):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    body = resp.json()
    assert "watch_recordings" in body
    assert "registrations_opened" in body
    assert "registrations" in body
    assert "funnel" in body
    assert "top_videos" in body
    assert "top_watchtime" in body
    assert body["funnel"][0]["name"] == "Opened"
    assert body["funnel"][0]["count"] == 100


def test_workshop_funnel_empty_when_no_data(mock_posthog_configured):
    with patch("src.analytics.posthog.get_trend", return_value=EMPTY_TREND), \
         patch("src.analytics.posthog.get_funnel", return_value=[]), \
         patch("src.analytics.posthog.get_top_breakdown", return_value=[]):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    assert resp.json()["funnel"] == []


# ── /content ──────────────────────────────────────────────────────────────────

def test_content_endpoint_shape(mock_posthog_configured):
    with patch("src.analytics.posthog.get_trend", return_value=SAMPLE_TREND), \
         patch("src.analytics.posthog.get_top_breakdown", return_value=SAMPLE_QUERIES):
        resp = admin_client().get("/api/v1/analytics/content")

    assert resp.status_code == 200
    body = resp.json()
    assert "resource_clicks" in body
    assert "topic_clicks" in body
    assert "top_resources" in body
    assert "top_topics" in body
    assert body["resource_clicks"]["total"] == 42


# ── /search ───────────────────────────────────────────────────────────────────

def test_search_endpoint_shape(mock_posthog_configured):
    with patch("src.analytics.posthog.get_trend", return_value=SAMPLE_TREND), \
         patch("src.analytics.posthog.get_top_breakdown", return_value=SAMPLE_QUERIES):
        resp = admin_client().get("/api/v1/analytics/search")

    assert resp.status_code == 200
    body = resp.json()
    assert "searches" in body
    assert "top_queries" in body
    assert body["top_queries"][0]["label"] == "FAFSA"


# ── Date range forwarding ─────────────────────────────────────────────────────

def test_date_params_forwarded_to_posthog(mock_posthog_configured):
    """date_from and date_to should be passed through to get_trend."""
    calls = []

    def spy_trend(api_key, project_id, event, **kwargs):
        calls.append({"date_from": kwargs.get("date_from"), "date_to": kwargs.get("date_to")})
        return EMPTY_TREND

    with patch("src.analytics.posthog.get_trend", side_effect=spy_trend), \
         patch("src.analytics.posthog.get_top_breakdown", return_value=[]):
        admin_client().get("/api/v1/analytics/content?date_from=2025-07-01&date_to=2026-06-30")

    assert any(c["date_from"] == "2025-07-01" for c in calls)
    assert any(c["date_to"] == "2026-06-30" for c in calls)
