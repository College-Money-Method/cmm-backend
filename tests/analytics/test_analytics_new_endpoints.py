"""Focused tests for new analytics endpoints: reach, workshops-detail, schools-health.

Uses in-process mocks only — no real DB or PostHog calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.analytics import posthog as ph
from src.analytics.schemas import TrendMetric
from src.auth.deps import require_counselor, require_admin
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.main import app

# ── Shared fixtures ───────────────────────────────────────────────────────────

SCHOOL_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SCHOOL_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

ADMIN_USER = CurrentUser(user_id=uuid.uuid4(), role="super_admin", school_id=None)
HUB_USER_A = CurrentUser(user_id=uuid.uuid4(), role="hub_user", school_id=SCHOOL_A)


def _mock_db() -> MagicMock:
    """Return a MagicMock DB session so tests never touch the real DB."""
    db = MagicMock()
    # db.get used by query_cache — return None (cache miss) by default
    db.get.return_value = None
    return db


def _admin_client() -> TestClient:
    app.dependency_overrides[require_counselor] = lambda: ADMIN_USER
    app.dependency_overrides[require_admin] = lambda: ADMIN_USER
    app.dependency_overrides[get_db] = _mock_db
    return TestClient(app)


def _hub_client() -> TestClient:
    app.dependency_overrides[require_counselor] = lambda: HUB_USER_A
    app.dependency_overrides[get_db] = _mock_db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()
    ph._cache.clear()


@pytest.fixture
def configured(mocker):
    s = __import__("src.config", fromlist=["settings"]).settings
    mocker.patch.object(s, "posthog_api_key", "phx_test")
    mocker.patch.object(s, "posthog_project_id", "99999")


# ── /reach — unit tests for benchmark math ────────────────────────────────────

class TestReachBenchmarkMath:
    """Pure unit tests for get_reach_benchmark — no DB needed."""

    def _make_school(self, enrollment: int, regs: int) -> dict:
        return {"enrollment_9_12": enrollment, "distinct_regs": regs}

    def _median(self, peers: list[dict]) -> float:
        pcts = sorted(p["distinct_regs"] / p["enrollment_9_12"] * 100 for p in peers)
        n = len(pcts)
        return (pcts[n // 2 - 1] + pcts[n // 2]) / 2 if n % 2 == 0 else pcts[n // 2]

    def test_fewer_than_3_peers_returns_none(self):
        """< 3 peers → benchmark must be None."""
        from src.analytics.postgres_queries import get_reach_benchmark

        db = MagicMock()
        db.get.return_value = MagicMock(enrollment_range="< 250")
        # Simulate only 2 peers returned
        db.execute.return_value.mappings.return_value.all.return_value = [
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 20},
            {"id": uuid.uuid4(), "enrollment_9_12": 200, "distinct_regs": 40},
        ]

        result = get_reach_benchmark(db, SCHOOL_A, "< 250")
        assert result is None

    def test_exactly_3_peers_returns_benchmark(self):
        from src.analytics.postgres_queries import get_reach_benchmark

        peers = [
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 10},   # 10%
            {"id": uuid.uuid4(), "enrollment_9_12": 200, "distinct_regs": 40},   # 20%
            {"id": uuid.uuid4(), "enrollment_9_12": 300, "distinct_regs": 90},   # 30%
        ]

        db = MagicMock()
        db.execute.return_value.mappings.return_value.all.return_value = peers

        result = get_reach_benchmark(db, SCHOOL_A, "< 250")
        assert result is not None
        assert result["peer_count"] == 3
        assert result["median_reach_pct"] == pytest.approx(20.0, rel=1e-3)

    def test_median_even_number_of_peers(self):
        from src.analytics.postgres_queries import get_reach_benchmark

        peers = [
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 10},   # 10%
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 20},   # 20%
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 30},   # 30%
            {"id": uuid.uuid4(), "enrollment_9_12": 100, "distinct_regs": 40},   # 40%
        ]

        db = MagicMock()
        db.execute.return_value.mappings.return_value.all.return_value = peers

        result = get_reach_benchmark(db, SCHOOL_A, "< 250")
        assert result is not None
        # median of [10, 20, 30, 40] = (20+30)/2 = 25
        assert result["median_reach_pct"] == pytest.approx(25.0, rel=1e-3)

    def test_above_median_flag_set_by_router(self):
        """above_median is set in the router by comparing school reach to peer median."""
        # reach_pct=35, median=25 → above_median=True
        from src.analytics.schemas import ReachBenchmark
        bm = ReachBenchmark(median_reach_pct=25.0, peer_count=4, above_median=35.0 > 25.0)
        assert bm.above_median is True

        bm2 = ReachBenchmark(median_reach_pct=25.0, peer_count=4, above_median=15.0 > 25.0)
        assert bm2.above_median is False


# ── /reach — endpoint smoke test ──────────────────────────────────────────────

class TestReachEndpoint:
    def test_super_admin_without_school_id_returns_400(self):
        client = _admin_client()
        resp = client.get("/api/v1/analytics/reach")
        assert resp.status_code == 400
        assert "school_id" in resp.json()["detail"].lower()

    def test_hub_user_gets_own_school(self, configured):
        with patch("src.analytics.router.get_reach_data") as mock_rd, \
             patch("src.analytics.router.get_reach_benchmark") as mock_rb:
            mock_rd.return_value = {
                "distinct_registrants": 50,
                "enrollment": 200,
                "reach_pct": 25.0,
                "enrollment_range": "< 250",
            }
            mock_rb.return_value = None  # <3 peers
            client = _hub_client()
            resp = client.get("/api/v1/analytics/reach")

        assert resp.status_code == 200
        body = resp.json()
        assert body["distinct_registrants"] == 50
        assert body["reach_pct"] == 25.0
        assert body["benchmark"] is None

    def test_reach_with_benchmark(self, configured):
        with patch("src.analytics.router.get_reach_data") as mock_rd, \
             patch("src.analytics.router.get_reach_benchmark") as mock_rb:
            mock_rd.return_value = {
                "distinct_registrants": 80,
                "enrollment": 200,
                "reach_pct": 40.0,
                "enrollment_range": "< 250",
            }
            mock_rb.return_value = {"median_reach_pct": 25.0, "peer_count": 5, "above_median": False}
            client = _hub_client()
            resp = client.get("/api/v1/analytics/reach")

        assert resp.status_code == 200
        body = resp.json()
        bm = body["benchmark"]
        assert bm is not None
        assert bm["median_reach_pct"] == 25.0
        assert bm["peer_count"] == 5
        assert bm["above_median"] is True  # 40 > 25


# ── /workshops-detail ─────────────────────────────────────────────────────────

class TestWorkshopsDetail:
    def test_requires_school_id_for_admin(self, configured):
        client = _admin_client()
        resp = client.get("/api/v1/analytics/workshops-detail")
        assert resp.status_code == 400

    def test_response_shape(self, configured):
        sample_row = {
            "webinar_id": "zoom-123",
            "workshop_name": "FAFSA 101",
            "start_datetime": "2026-06-01T18:00:00+00:00",
            "registered": 40,
            "attended_live": 30,
            "no_show": 10,
            "joined_without_reg": 5,
            "recording_views": 0,
            "avg_percent_watched": None,
            "_webinar_id_raw": uuid.uuid4(),
        }

        # Views (video_view) and avg % watched (video_session_end) are now two
        # separate per-webinar reads — return the right shape for each.
        def hogql(api_key, project_id, query, **kwargs):
            if "GROUP BY properties.object_id" in query and "video_view" in query:
                return [["zoom-123", 7]]        # recording VIEWS
            if "GROUP BY properties.object_id" in query and "percent_watched" in query:
                return [["zoom-123", 0.65]]     # avg % watched
            if "countIf(event = '$pageview') AS visits" in query:
                return [[100, 7, 20]]           # site totals
            return []

        with patch("src.analytics.router.get_webinars_for_school_in_range", return_value=[sample_row]), \
             patch("src.analytics.router.get_workshops_detail_totals", return_value={
                 "registered": 40, "attended_live": 30, "no_show": 10, "recording_views": 0
             }), \
             patch("src.analytics.posthog.get_hogql_query", side_effect=hogql):
            client = _hub_client()
            resp = client.get("/api/v1/analytics/workshops-detail?date_from=-30d")

        assert resp.status_code == 200
        body = resp.json()
        assert "webinars" in body and "totals" in body
        w = body["webinars"][0]
        assert w["registered"] == 40
        assert w["attended_live"] == 30
        assert w["no_show"] == 10
        assert w["joined_without_reg"] == 5
        # PostHog recording_views merged in
        assert w["recording_views"] == 7
        assert abs(w["avg_percent_watched"] - 0.65) < 0.001

    def test_no_webinars_returns_empty(self, configured):
        with patch("src.analytics.router.get_webinars_for_school_in_range", return_value=[]), \
             patch("src.analytics.router.get_workshops_detail_totals", return_value={
                 "registered": 0, "attended_live": 0, "no_show": 0, "recording_views": 0
             }):
            client = _hub_client()
            resp = client.get("/api/v1/analytics/workshops-detail?date_from=-30d")

        assert resp.status_code == 200
        body = resp.json()
        assert body["webinars"] == []
        assert body["totals"]["registered"] == 0


# ── /admin/schools-health — window logic ─────────────────────────────────────

class TestSchoolsHealthWindowLogic:
    """Verify stalled/quiet/declining helper logic via mocked DB results."""

    def test_stalled_activation_shape(self):
        from src.analytics.postgres_queries_admin import get_stalled_activations

        now = datetime.now(timezone.utc)
        school_mock = MagicMock()
        school_mock.id = SCHOOL_A
        school_mock.name = "Stalled High"
        school_mock.state = "CA"
        school_mock.enrollment_range = "< 250"
        school_mock.created_at = now - timedelta(days=45)

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [school_mock]

        rows = get_stalled_activations(db)
        assert len(rows) == 1
        assert rows[0]["name"] == "Stalled High"
        assert rows[0]["state"] == "CA"
        assert "created_at" in rows[0]

    def test_quiet_school_shape(self):
        from src.analytics.postgres_queries_admin import get_quiet_schools

        db = MagicMock()
        row_mock = MagicMock()
        row_mock.School.id = SCHOOL_A
        row_mock.School.name = "Quiet High"
        row_mock.School.state = "TX"
        row_mock.School.enrollment_range = "250-500"
        row_mock.recent_regs = 0
        row_mock.prior_regs = 15
        db.execute.return_value.all.return_value = [row_mock]

        rows = get_quiet_schools(db)
        assert len(rows) == 1
        assert rows[0]["recent_regs"] == 0
        assert rows[0]["prior_regs"] == 15

    def test_declining_school_shape(self):
        from src.analytics.postgres_queries_admin import get_declining_schools

        db = MagicMock()
        row_mock = MagicMock()
        row_mock.School.id = SCHOOL_B
        row_mock.School.name = "Declining High"
        row_mock.School.state = "NY"
        row_mock.School.enrollment_range = ">500"
        row_mock.recent_regs = 4
        row_mock.prior_regs = 20  # 4 < 50% of 20 → declining
        db.execute.return_value.all.return_value = [row_mock]

        rows = get_declining_schools(db)
        assert len(rows) == 1
        assert rows[0]["recent_regs"] == 4
        assert rows[0]["prior_regs"] == 20


# ── /admin/schools-health — endpoint smoke ───────────────────────────────────

class TestSchoolsHealthEndpoint:
    def test_response_shape(self, configured):
        with patch("src.analytics.admin_router.get_stalled_activations", return_value=[]), \
             patch("src.analytics.admin_router.get_quiet_schools", return_value=[]), \
             patch("src.analytics.admin_router.get_declining_schools", return_value=[]):
            client = _admin_client()
            resp = client.get("/api/v1/analytics/admin/schools-health")

        assert resp.status_code == 200
        body = resp.json()
        assert "stalled_activations" in body
        assert "quiet_schools" in body
        assert "declining_schools" in body


# ── /workshop-timeline — DB-sourced registrations + attendees ────────────────

class TestWorkshopTimeline:
    """Registrations + attendees are DB-authoritative (by registration_time),
    windowed server-side; PostHog only supplies the engagement/video/resource
    series. Guards against regressing back to PostHog-based registrations."""

    def test_registrations_and_attendees_from_db(self, configured):
        wid = uuid.uuid4()
        webinar = {
            "webinar_id": str(wid),
            "workshop_name": "FAFSA 101",
            "start_datetime": datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc),
        }
        # DB registrations by day (both inside the window) + windowed attendees.
        reg_by_day = {"2026-05-26": 3, "2026-06-01": 5}
        with patch("src.analytics.router.get_webinar_by_id", return_value=webinar), \
             patch(
                 "src.analytics.router.get_webinar_windowed_registrations",
                 return_value=(reg_by_day, 30),
             ) as mock_reg, \
             patch(
                 "src.analytics.posthog_batched.get_windowed_trends_by_webinar",
                 return_value={},
             ):
            client = _hub_client()
            resp = client.get(
                f"/api/v1/analytics/workshop-timeline?webinar_id={wid}"
                "&date_from=2026-05-25&date_to=2026-06-08"
            )

        assert resp.status_code == 200
        body = resp.json()
        # Registrations series is DB-sourced: total = sum of the per-day counts.
        assert body["registrations"]["total"] == 8
        assert body["attendees"] == 30
        # Engagement series still zero-filled from the (empty) PostHog result.
        assert body["detail_views"]["total"] == 0
        assert mock_reg.called


# ── /workshop-engagement — school scoping (shared webinars) ──────────────────

class TestWorkshopEngagementSchoolScoping:
    """CMM webinars are shared across schools, so a webinar_id's events span many
    schools. The resources query MUST scope to the counselor's school — otherwise
    the card aggregates every school's activity (the bug where the 'Workshop
    Resources Used' card overcounted vs. the school-scoped tile)."""

    def test_query_includes_school_clause(self, configured):
        wid = uuid.uuid4()
        webinar = {
            "webinar_id": str(wid),
            "workshop_name": "FAFSA 101",
            "start_datetime": datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc),
        }
        captured: list[str] = []

        def spy(api_key, project_id, query, **kwargs):
            captured.append(query)
            return []

        with patch("src.analytics.router.get_webinar_by_id", return_value=webinar), \
             patch("src.analytics.posthog.get_hogql_query", side_effect=spy):
            resp = _hub_client().get(
                f"/api/v1/analytics/workshop-engagement?webinar_id={wid}"
                "&date_from=2026-05-04&date_to=2026-06-29"
            )

        assert resp.status_code == 200
        assert len(captured) == 1  # resources_used only
        assert f"properties.school_id = '{SCHOOL_A}'" in captured[0], captured


# ── Smoke: app imports cleanly with admin router mounted ─────────────────────

def test_app_imports_and_has_admin_routes():
    from src.main import app as _app
    routes = [r.path for r in _app.routes]
    assert any("/api/v1/analytics/admin/pulse" in r for r in routes)
    assert any("/api/v1/analytics/admin/schools-health" in r for r in routes)
    assert any("/api/v1/analytics/admin/big-picture" in r for r in routes)
    assert any("/api/v1/analytics/admin/whats-working" in r for r in routes)
    assert any("/api/v1/analytics/admin/geographic" in r for r in routes)


# ── Forced-refresh partial-outage protection (workshops-detail helper) ─────────

def test_workshops_detail_ph_force_partial_outage_keeps_complete_cache():
    """A forced refresh (force=True) bypasses the cache read and re-queries. If
    PostHog PARTIALLY fails, the assembled partial result must NOT overwrite a
    previously-complete cache entry — the stale-but-complete entry is served."""
    from src.analytics import posthog as ph
    from src.analytics.router import _query_workshops_detail_ph

    ph._cache.clear()
    sid = str(SCHOOL_A)

    def all_ok(api_key, project_id, query, **kwargs):
        if "countIf(event = '$pageview') AS visits" in query:
            return [[100, 50, 20]]                       # site totals
        if "GROUP BY properties.object_id" in query and "video_view" in query:
            return [["w1", 5]]                           # recording VIEWS (video_view)
        if "GROUP BY properties.object_id" in query and "percent_watched" in query:
            return [["w1", 80.0]]                        # avg % watched (video_session_end)
        if "workshop_detail_view" in query:
            return [["w1", 3]]                           # detail views
        if "properties.via = 'workshop'" in query:
            return [["w1", 2]]                           # resource views
        return []

    # 1) Cold call with everything OK → populates the in-process cache (db=None).
    with patch("src.analytics.posthog.get_hogql_query", side_effect=all_ok):
        good = _query_workshops_detail_ph("k", "p", None, sid, "-30d", None, None)
    assert good[2] == {"w1": 3}      # detail_map complete
    assert len(ph._cache) == 1

    # 2) Forced refresh while the detail-views query is down (partial outage).
    def partial(api_key, project_id, query, **kwargs):
        if "workshop_detail_view" in query:
            raise RuntimeError("posthog down")
        return all_ok(api_key, project_id, query)

    with patch("src.analytics.posthog.get_hogql_query", side_effect=partial):
        forced = _query_workshops_detail_ph("k", "p", None, sid, "-30d", None, None, force=True)

    # Stale COMPLETE entry served — detail_map preserved, not clobbered to {}.
    assert forced[2] == {"w1": 3}
    assert forced[0] == good[0]      # site totals preserved


# ── /topic-engagement — Grade → Goal → Topic tree with per-topic metrics ───────

TOPIC_1 = "11111111-1111-1111-1111-111111111111"
TOPIC_2 = "22222222-2222-2222-2222-222222222222"

_SAMPLE_TREE = (
    "lincoln-high",
    [
        {
            "grade": 9,
            "label": "9th Grade",
            "page_title": "Learn How Financial Aid Works",
            "goals": [
                {
                    "goal_id": "99999999-9999-9999-9999-999999999999",
                    "name": "Build a college list",
                    "topics": [
                        {"topic_id": TOPIC_1, "title": "Picking schools", "slug": "picking-schools"},
                        {"topic_id": TOPIC_2, "title": "Costs 101", "slug": "costs-101"},
                    ],
                }
            ],
        }
    ],
)


class TestTopicEngagement:
    def test_tree_metrics_and_rollups(self, configured):
        # engagement + video_views per topic id; TOPIC_2 has no activity at all.
        def hogql(api_key, project_id, query, **kwargs):
            assert "topic_viewed" in query and "video_view" in query
            assert TOPIC_1 in query  # filtered by the tree's topic ids
            return [[TOPIC_1, 12, 4]]

        with patch("src.analytics.router.get_school_topic_tree", return_value=_SAMPLE_TREE), \
             patch("src.analytics.posthog.get_hogql_query", side_effect=hogql):
            client = _hub_client()
            resp = client.get("/api/v1/analytics/topic-engagement?date_from=-30d")

        assert resp.status_code == 200
        body = resp.json()
        assert body["school_slug"] == "lincoln-high"
        grade = body["grades"][0]
        assert grade["page_title"] == "Learn How Financial Aid Works"
        goal = grade["goals"][0]
        # Topics with no activity are KEPT (zeros) so coverage stays visible.
        assert [t["engagement"] for t in goal["topics"]] == [12, 0]
        assert [t["video_views"] for t in goal["topics"]] == [4, 0]
        # Rollups: goal → grade → tree
        assert (goal["engagement"], goal["video_views"]) == (12, 4)
        assert (grade["engagement"], grade["video_views"]) == (12, 4)
        assert (body["engagement"], body["video_views"]) == (12, 4)

    def test_posthog_error_still_returns_tree(self, configured):
        with patch("src.analytics.router.get_school_topic_tree", return_value=_SAMPLE_TREE), \
             patch("src.analytics.posthog.get_hogql_query", side_effect=RuntimeError("posthog down")):
            client = _hub_client()
            resp = client.get("/api/v1/analytics/topic-engagement?date_from=-30d")

        assert resp.status_code == 200
        body = resp.json()
        topics = body["grades"][0]["goals"][0]["topics"]
        assert len(topics) == 2
        assert all(t["engagement"] == 0 and t["video_views"] == 0 for t in topics)

    def test_no_topics_skips_posthog(self, configured):
        with patch("src.analytics.router.get_school_topic_tree", return_value=(None, [])), \
             patch("src.analytics.posthog.get_hogql_query") as q:
            client = _hub_client()
            resp = client.get("/api/v1/analytics/topic-engagement?date_from=-30d")

        assert resp.status_code == 200
        assert resp.json()["grades"] == []
        q.assert_not_called()
