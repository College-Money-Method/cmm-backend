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
from src.analytics.schemas import RankedContentRow, TopBreakdown, TrendMetric
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
    assert "dau" in body and "hub_dau" in body and "sign_ins" in body
    assert body["dau"]["total"] == 42


def test_overview_dau_split_by_surface(mock_posthog_configured):
    """`dau` is Resource Center only and `hub_dau` is the Hub — a single
    unqualified $pageview DAU also swept in CMM staff browsing /admin."""
    series_seen = []

    def spy(api_key, project_id, series, **kwargs):
        series_seen.extend(series)
        return {s["key"]: SAMPLE_TREND for s in series}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=spy):
        resp = admin_client().get("/api/v1/analytics/overview")

    assert resp.status_code == 200
    by_key = {s["key"]: s for s in series_seen}
    assert by_key["dau"]["extra_filter"] == " AND properties.$pathname LIKE '/school/%'"
    assert by_key["hub_dau"]["extra_filter"] == " AND properties.$pathname LIKE '/hub%'"
    assert "extra_filter" not in by_key["sign_ins"]


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


def test_overview_refresh_param_forwards_force_refresh(mock_posthog_configured):
    """?refresh=true must forward force_refresh=True to the batched helper so the
    Refresh button bypasses the 60-min cache."""
    seen = []

    def spy(api_key, project_id, series, **kwargs):
        seen.append(kwargs.get("force_refresh"))
        return {s["key"]: SAMPLE_TREND for s in series}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=spy):
        client = counselor_client()
        resp_default = client.get("/api/v1/analytics/overview")
        resp_refresh = client.get("/api/v1/analytics/overview?refresh=true")

    assert resp_default.status_code == 200 and resp_refresh.status_code == 200
    assert seen == [False, True]


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
    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends()), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=fake_breakdowns()):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    body = resp.json()
    assert "watch_recordings" in body
    assert "registrations_opened" in body
    assert "registrations" in body
    assert "funnel" not in body
    assert "milestone_dropoff" not in body  # removed — recording drop-off card retired
    assert "top_videos" in body
    assert "top_watchtime" in body


def test_workshop_video_breakdowns_filter_to_workshop_videos(mock_posthog_configured):
    """video_view/video_session_end also fire for topic, resource and welcome
    videos — without the object_type filter the workshop cards rank those."""
    specs_seen = []

    def breakdowns(api_key, project_id, specs, **kwargs):
        specs_seen.extend(specs)
        return {sp["key"]: SAMPLE_QUERIES for sp in specs}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=fake_trends()), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=breakdowns):
        resp = admin_client().get("/api/v1/analytics/workshop")

    assert resp.status_code == 200
    assert {sp["key"] for sp in specs_seen} == {"top_videos", "top_watchtime"}
    for sp in specs_seen:
        assert sp["extra_filter"] == " AND properties.object_type = 'workshop'"


# ── /content ──────────────────────────────────────────────────────────────────

def test_content_endpoint_shape(mock_posthog_configured):
    # Number-only content page: videos (views + avg % watched), resources, topics.
    # The Content Engagement tiles read the aggregate totals (one extra query).
    # Resource rows are id-keyed and named from Postgres (resolve_asset_rows is
    # unit-tested in test_resource_breakdown_queries.py), so it's stubbed here.
    resolved = [RankedContentRow(id="0f8f-asset", name="FAFSA Checklist", count=30)]
    with patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=fake_breakdowns()), \
         patch("src.analytics.router.resolve_asset_rows", return_value=resolved), \
         patch("src.analytics.router.get_school_slug", return_value="lincoln-high"), \
         patch("src.analytics.posthog.get_hogql_query", return_value=[[12, 34, 56]]):
        resp = admin_client().get("/api/v1/analytics/content")

    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body and "resources" in body and "topics" in body
    # Other Videos card — resolved to named rows (id/name/count), NOT raw labels.
    # SAMPLE_QUERIES labels aren't UUIDs, so they resolve to unlinked "removed" rows.
    assert body["other_videos"][0]["count"] == 30
    assert body["other_videos"][0]["id"] is None
    # videos merge the count breakdown (view_count) with the avg-% breakdown
    assert body["videos"][0]["name"] == "FAFSA"
    assert body["videos"][0]["view_count"] == 30
    assert body["videos"][0]["avg_percent_watched"] == 30
    # Resources carry the id the UI links with, plus the current name.
    assert body["resources"][0] == {"id": "0f8f-asset", "name": "FAFSA Checklist", "count": 30}
    assert body["school_slug"] == "lincoln-high"
    assert body["topics"][0]["label"] == "FAFSA"
    # Content Engagement totals — video_views is topic-scoped (object_type='topic').
    assert body["totals"] == {"topic_engagement": 12, "resources_used": 34, "video_views": 56}


def test_content_video_views_scoped_to_topic(mock_posthog_configured):
    """The "Topic Video Views" tile + video breakdowns count only Topic Page
    videos (object_type='topic'), not all Resource Center videos."""
    seen = {}
    captured_hogql = []

    def spy(api_key, project_id, specs, **kwargs):
        seen.update({sp["key"]: sp for sp in specs})
        return {sp["key"]: [] for sp in specs}

    def hogql(api_key, project_id, query, **kwargs):
        captured_hogql.append(query)
        return [[0, 0, 0]]

    with patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=spy), \
         patch("src.analytics.posthog.get_hogql_query", side_effect=hogql):
        resp = admin_client().get("/api/v1/analytics/content")

    assert resp.status_code == 200
    # Video breakdowns filter to topic videos.
    assert seen["video_views"]["extra_filter"] == " AND properties.object_type = 'topic'"
    assert seen["video_pct"]["extra_filter"] == " AND properties.object_type = 'topic'"
    # Other Videos card is the complement: resource-embedded + welcome videos.
    assert seen["other_videos"]["extra_filter"] == " AND properties.object_type IN ('resource', 'welcome')"
    # Totals video_views count is topic-scoped in HogQL.
    assert any(
        "video_view' AND properties.object_type = 'topic'" in q for q in captured_hogql
    )


def test_content_groups_resources_by_asset_id(mock_posthog_configured):
    """Grouping by asset_name merged same-named assets and split renamed ones."""
    seen = {}

    def spy(api_key, project_id, specs, **kwargs):
        seen.update({sp["key"]: sp for sp in specs})
        return {sp["key"]: [] for sp in specs}

    with patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=spy), \
         patch("src.analytics.posthog.get_hogql_query", return_value=[[0, 0, 0]]):
        resp = admin_client().get("/api/v1/analytics/content")

    assert resp.status_code == 200
    assert seen["resources"]["prop"] == "asset_id"


def test_content_breakdown_paginates(mock_posthog_configured):
    # 21 rows for limit=20 → has_more True, trimmed to 20.
    rows = [[f"Resource {i}", float(100 - i)] for i in range(21)]
    # Name resolution is stubbed to a pass-through so ordering stays readable.
    def passthrough(_db, page):
        return [RankedContentRow(id=None, name=r.label, count=int(r.count)) for r in page]

    with patch("src.analytics.posthog.get_hogql_query", return_value=rows), \
         patch("src.analytics.router.resolve_asset_rows", side_effect=passthrough):
        resp = admin_client().get("/api/v1/analytics/content-breakdown?kind=resources&limit=20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is True
    assert len(body["rows"]) == 20
    assert body["rows"][0]["name"] == "Resource 0"


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


def test_search_counts_both_search_surfaces(mock_posthog_configured):
    """Site search fires from the global dialog AND the results page; counting
    only search_query missed nearly every search."""
    seen = []

    def trend_spy(api_key, project_id, specs, **kwargs):
        seen.extend(specs)
        return {sp["key"]: SAMPLE_TREND for sp in specs}

    def breakdown_spy(api_key, project_id, specs, **kwargs):
        seen.extend(specs)
        return {sp["key"]: SAMPLE_QUERIES for sp in specs}

    with patch("src.analytics.posthog_batched.get_batched_trends", side_effect=trend_spy), \
         patch("src.analytics.posthog_batched.get_batched_breakdowns", side_effect=breakdown_spy):
        resp = admin_client().get("/api/v1/analytics/search")

    assert resp.status_code == 200
    by_key = {s["key"]: s for s in seen}
    for key in ("searches", "top_queries"):
        assert by_key[key]["event"] == ["search_query", "global_search_performed"]


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


def test_batched_trends_applies_extra_filter_and_event_lists(mock_posthog_configured):
    """Per-series extra_filter narrows only its own column; an `event` list
    counts several events as one series (site search across two surfaces)."""
    from src.analytics.posthog_batched import get_batched_trends

    seen = {}
    with patch("src.analytics.posthog.get_hogql_query",
               side_effect=lambda k, p, q, **kw: seen.setdefault("q", q) and []):
        get_batched_trends(
            "key", "123",
            [
                {"key": "dau", "event": "$pageview", "math": "dau",
                 "extra_filter": " AND properties.$pathname LIKE '/school/%'"},
                {"key": "searches", "event": ["search_query", "global_search_performed"]},
            ],
            date_from="2026-06-09", date_to="2026-06-09",
        )

    q = seen["q"]
    assert "uniqIf(person_id, event = '$pageview' AND properties.$pathname LIKE '/school/%')" in q
    assert "countIf(event IN ('search_query', 'global_search_performed'))" in q
    # every referenced event must survive the outer event IN (...) prefilter
    for ev in ("$pageview", "search_query", "global_search_performed"):
        assert f"'{ev}'" in q.split("GROUP BY")[0]


def test_batched_breakdowns_applies_extra_filter(mock_posthog_configured):
    from src.analytics.posthog_batched import get_batched_breakdowns

    seen = {}
    with patch("src.analytics.posthog.get_hogql_query",
               side_effect=lambda k, p, q, **kw: seen.setdefault("q", q) and []):
        get_batched_breakdowns(
            "key", "123",
            [{"key": "top_videos", "event": "video_view", "prop": "object_name",
              "extra_filter": " AND properties.object_type = 'workshop'"}],
            date_from="2026-06-09", date_to="2026-06-09",
        )

    q = seen["q"]
    # extra_filter narrows the spec's own multiIf arm, not the whole scan
    assert "event = 'video_view' AND properties.object_type = 'workshop', 'top_videos'" in q
    assert "event = 'video_view' AND properties.object_type = 'workshop', toString(properties.object_name)" in q
    assert "UNION ALL" not in q  # one spec → one scan


def test_batched_breakdowns_disjoint_specs_use_one_scan(mock_posthog_configured):
    """Specs on different events collapse into a single pass over `events`."""
    from src.analytics.posthog_batched import get_batched_breakdowns

    seen = {}
    with patch("src.analytics.posthog.get_hogql_query",
               side_effect=lambda k, p, q, **kw: seen.setdefault("q", q) and []):
        get_batched_breakdowns(
            "key", "123",
            [
                {"key": "video_views", "event": "video_view", "prop": "object_name"},
                {"key": "video_pct", "event": "video_session_end", "prop": "object_name",
                 "math": "avg", "math_prop": "percent_watched"},
                {"key": "resources", "event": "resource_viewed", "prop": "asset_id"},
            ],
            date_from="2026-06-09", date_to="2026-06-09",
        )

    q = seen["q"]
    assert "UNION ALL" not in q
    assert q.count("FROM events") == 1
    # every event survives the outer prefilter
    for ev in ("video_view", "video_session_end", "resource_viewed"):
        assert f"'{ev}'" in q
    # only the avg spec reads percent_watched; the count specs stay on count()
    assert "if(kind IN ('video_pct'), avg(mval), toFloat(count()))" in q


def test_batched_breakdowns_overlapping_events_fall_back_to_union(mock_posthog_configured):
    """Two specs on the SAME event can't share a multiIf — one row would only
    reach the first arm, so the builder keeps them on separate scans."""
    from src.analytics.posthog_batched import get_batched_breakdowns

    seen = {}
    with patch("src.analytics.posthog.get_hogql_query",
               side_effect=lambda k, p, q, **kw: seen.setdefault("q", q) and []):
        get_batched_breakdowns(
            "key", "123",
            [
                {"key": "by_name", "event": "video_view", "prop": "object_name"},
                {"key": "by_type", "event": "video_view", "prop": "object_type"},
            ],
            date_from="2026-06-09", date_to="2026-06-09",
        )

    assert "UNION ALL" in seen["q"]


def test_batched_helpers_reject_bad_event_names_in_lists(mock_posthog_configured):
    """List form must validate every name — not just the first."""
    from src.analytics.posthog_batched import get_batched_trends

    with pytest.raises(ValueError):
        get_batched_trends(
            "key", "123",
            [{"key": "x", "event": ["search_query", "evil'; DROP"]}],
            date_from="2026-06-09", date_to="2026-06-09",
        )
