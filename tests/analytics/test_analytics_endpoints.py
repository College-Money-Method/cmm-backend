"""Integration tests for /api/v1/analytics/* endpoints via FastAPI TestClient.

The 4 dashboard endpoints use src.analytics.posthog_batched — one batched
trends call + one batched breakdowns call per endpoint. Mocks build result
dicts from the series/spec keys the endpoint passes in.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.analytics import posthog as ph
from src.analytics.schemas import TopBreakdown, TrendMetric
from src.auth.deps import require_counselor
from src.auth.schemas import CurrentUser
from src.main import app

# ── Fixtures ──────────────────────────────────────────────────────────────────

SCHOOL_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SCHOOL_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

ADMIN = CurrentUser(user_id=uuid.uuid4(), role="super_admin", school_id=None)
COUNSELOR_A = CurrentUser(user_id=uuid.uuid4(), role="hub_user", school_id=SCHOOL_A)

EMPTY_TREND = TrendMetric(total=0, data=[], days=[])
SAMPLE_TREND = TrendMetric(total=42, data=[5.0, 10.0, 27.0], days=["2026-06-09", "2026-06-10", "2026-06-11"])
SAMPLE_QUERIES = [TopBreakdown(label="FAFSA", count=30), TopBreakdown(label="grants", count=20)]


def fake_trends(trend=SAMPLE_TREND):
    """Batched-trends mock: one metric per requested series key."""
    def _impl(api_key, project_id, series, **kwargs):
        return {s["key"]: trend for s in series}
    return _impl


def fake_breakdowns(rows=SAMPLE_QUERIES):
    """Batched-breakdowns mock: same rows for every requested spec key."""
    def _impl(api_key, project_id, specs, **kwargs):
        return {sp["key"]: rows for sp in specs}
    return _impl


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
    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends()):
        client = admin_client()
        resp = client.get("/api/v1/analytics/overview")

    assert resp.status_code == 200
    body = resp.json()
    assert "dau" in body and "sign_ins" in body
    assert body["dau"]["total"] == 42


def test_overview_admin_with_school_id(mock_posthog_configured):
    """Admin can pass school_id — it should be forwarded to the batched helper."""
    calls = []

    def spy(api_key, project_id, series, **kwargs):
        calls.append(kwargs.get("school_id"))
        return {s["key"]: SAMPLE_TREND for s in series}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=spy):
        client = admin_client()
        resp = client.get(f"/api/v1/analytics/overview?school_id={SCHOOL_A}")

    assert resp.status_code == 200
    assert all(str(c) == str(SCHOOL_A) for c in calls if c)


def test_overview_counselor_ignores_school_param(mock_posthog_configured):
    """Counselors are always scoped to their own school, never the query param."""
    calls = []

    def spy(api_key, project_id, series, **kwargs):
        calls.append(kwargs.get("school_id"))
        return {s["key"]: SAMPLE_TREND for s in series}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=spy):
        client = counselor_client()
        # Passing school_B in param should be overridden by counselor's school_A
        resp = client.get(f"/api/v1/analytics/overview?school_id={SCHOOL_B}")

    assert resp.status_code == 200
    for school_id_used in calls:
        if school_id_used:
            assert str(school_id_used) == str(SCHOOL_A)


# ── /workshop ─────────────────────────────────────────────────────────────────

def test_workshop_endpoint_shape(mock_posthog_configured):
    dropoff = [TopBreakdown(label="10%", count=40), TopBreakdown(label="50%", count=25)]

    def breakdowns(api_key, project_id, specs, **kwargs):
        return {sp["key"]: (dropoff if sp["key"] == "milestone_dropoff" else SAMPLE_QUERIES) for sp in specs}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends()), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=breakdowns):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    body = resp.json()
    assert "watch_recordings" in body
    assert "registrations_opened" in body
    assert "registrations" in body
    assert "funnel" not in body  # removed — replaced by milestone_dropoff
    assert "top_videos" in body
    assert "top_watchtime" in body
    assert body["milestone_dropoff"][0]["label"] == "10%"
    assert body["milestone_dropoff"][0]["count"] == 40


def test_workshop_milestone_dropoff_empty_when_no_data(mock_posthog_configured):
    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends(EMPTY_TREND)), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=fake_breakdowns([])):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    assert resp.json()["milestone_dropoff"] == []


# ── /content ──────────────────────────────────────────────────────────────────

def test_content_endpoint_shape(mock_posthog_configured):
    # Number-only content page: videos (views + avg % watched), resources, topics.
    with patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=fake_breakdowns()):
        resp = admin_client().get("/api/v1/analytics/content")

    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body and "resources" in body and "topics" in body
    # videos merge the count breakdown (view_count) with the avg-% breakdown
    assert body["videos"][0]["name"] == "FAFSA"
    assert body["videos"][0]["view_count"] == 30
    assert body["videos"][0]["avg_percent_watched"] == 30
    assert body["resources"][0]["label"] == "FAFSA"
    assert body["topics"][0]["label"] == "FAFSA"


def test_content_breakdown_paginates(mock_posthog_configured):
    # 21 rows for limit=20 → has_more True, trimmed to 20.
    rows = [[f"Resource {i}", float(100 - i)] for i in range(21)]
    with patch("src.analytics.posthog.get_hogql_query", return_value=rows):
        resp = admin_client().get("/api/v1/analytics/content-breakdown?kind=resources&limit=20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is True
    assert len(body["rows"]) == 20
    assert body["rows"][0]["label"] == "Resource 0"


def test_content_breakdown_rejects_invalid_kind(mock_posthog_configured):
    resp = admin_client().get("/api/v1/analytics/content-breakdown?kind=bogus")
    assert resp.status_code == 400


# ── /search ───────────────────────────────────────────────────────────────────

def test_search_endpoint_shape(mock_posthog_configured):
    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends()), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=fake_breakdowns()):
        resp = admin_client().get("/api/v1/analytics/search")

    assert resp.status_code == 200
    body = resp.json()
    assert "searches" in body
    assert "top_queries" in body
    assert body["top_queries"][0]["label"] == "FAFSA"


# ── Date range forwarding ─────────────────────────────────────────────────────

def test_date_params_forwarded_to_posthog(mock_posthog_configured):
    """date_from and date_to should be passed through to the batched helpers."""
    calls = []

    def spy(api_key, project_id, specs, **kwargs):
        calls.append({"date_from": kwargs.get("date_from"), "date_to": kwargs.get("date_to")})
        return {sp["key"]: [] for sp in specs}

    with patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=spy):
        admin_client().get("/api/v1/analytics/content?date_from=2025-07-01&date_to=2026-06-30")

    assert any(c["date_from"] == "2025-07-01" for c in calls)
    assert any(c["date_to"] == "2026-06-30" for c in calls)


# ── Batched helper unit tests ─────────────────────────────────────────────────

def test_batched_trends_zero_fills_days(mock_posthog_configured):
    """Days with no events must appear as zeros, aligned across series."""
    from src.analytics.posthog_batched import get_batched_trends

    rows = [["2026-06-10T00:00:00Z", 5, 2]]  # only one of three days has data
    with patch("src.analytics.posthog.get_hogql_query", return_value=rows):
        result = get_batched_trends(
            "key", "123",
            [{"key": "a", "event": "e1"}, {"key": "b", "event": "e2"}],
            date_from="2026-06-09", date_to="2026-06-11",
        )

    assert result["a"].days == ["2026-06-09", "2026-06-10", "2026-06-11"]
    assert result["a"].data == [0.0, 5.0, 0.0]
    assert result["b"].data == [0.0, 2.0, 0.0]
    assert result["a"].total == 5


def test_batched_breakdowns_orders_and_limits(mock_posthog_configured):
    """count specs sort desc + limit; label_num specs keep numeric label order."""
    from src.analytics.posthog_batched import get_batched_breakdowns

    rows = [
        ["top", "Small", 1.0], ["top", "Big", 9.0], ["top", "Mid", 5.0],
        ["ms", "100", 3.0], ["ms", "10", 20.0], ["ms", "50", 8.0],
    ]
    with patch("src.analytics.posthog.get_hogql_query", return_value=rows):
        result = get_batched_breakdowns(
            "key", "123",
            [
                {"key": "top", "event": "e1", "prop": "name", "limit": 2},
                {"key": "ms", "event": "recording_progress", "prop": "milestone_pct", "order": "label_num"},
            ],
        )

    assert [b.label for b in result["top"]] == ["Big", "Mid"]  # desc, limited to 2
    assert [b.label for b in result["ms"]] == ["10%", "50%", "100%"]  # milestone order
