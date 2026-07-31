"""Served-cache-age tracking — the `cached_at` the hub's Refresh control reads.

Endpoints report when their numbers were ACTUALLY read from PostHog so the UI can
label real data age and disable Refresh until the entry is worth re-querying. The
timestamp is collected on the request-scoped Session as cache entries are served.
"""

from datetime import datetime, timedelta, timezone

from src.analytics.query_cache import (
    AnalyticsQueryCache,
    db_cache_get,
    db_cache_get_stale,
    oldest_cache_hit,
)

TTL = timedelta(minutes=60)


class _FakeSession:
    """Minimal stand-in: the helpers only need .get() and .info (a dict)."""

    def __init__(self, *rows: AnalyticsQueryCache):
        self._rows = {r.key: r for r in rows}
        self.info: dict = {}

    def get(self, _model, key, **_kwargs):
        # **_kwargs swallows populate_existing=True (the readers bypass the
        # identity map so single_flight's post-wait re-check sees fresh rows).
        return self._rows.get(key)


def _row(key: str, age: timedelta) -> AnalyticsQueryCache:
    row = AnalyticsQueryCache(key=key, payload={"v": key})
    row.fetched_at = datetime.now(timezone.utc) - age
    return row


class TestOldestCacheHit:
    def test_none_until_something_is_served(self):
        db = _FakeSession(_row("a", timedelta(minutes=5)))
        # Nothing read yet — a request that computes everything fresh reports
        # None, which the client renders as "just now".
        assert oldest_cache_hit(db) is None

    def test_within_ttl_hit_is_recorded(self):
        db = _FakeSession(_row("a", timedelta(minutes=42)))
        assert db_cache_get(db, "a", TTL) == {"v": "a"}
        age = datetime.now(timezone.utc) - oldest_cache_hit(db)
        assert timedelta(minutes=41) < age < timedelta(minutes=43)

    def test_oldest_of_several_wins(self):
        """A response assembled from several cached sub-queries is only as fresh
        as its OLDEST part."""
        db = _FakeSession(_row("a", timedelta(minutes=5)), _row("b", timedelta(minutes=50)))
        db_cache_get(db, "a", TTL)
        db_cache_get(db, "b", TTL)
        age = datetime.now(timezone.utc) - oldest_cache_hit(db)
        assert age > timedelta(minutes=49)

    def test_expired_entry_is_not_a_hit(self):
        db = _FakeSession(_row("a", timedelta(minutes=90)))
        assert db_cache_get(db, "a", TTL) is None  # past TTL → caller recomputes
        assert oldest_cache_hit(db) is None

    def test_stale_serve_reports_its_real_age(self):
        """Stale-on-error is exactly when honest age matters most — the UI shows
        hours-old data during a PostHog outage instead of claiming freshness."""
        db = _FakeSession(_row("a", timedelta(hours=3)))
        assert db_cache_get_stale(db, "a") == {"v": "a"}
        assert datetime.now(timezone.utc) - oldest_cache_hit(db) > timedelta(hours=2)

    def test_missing_row_records_nothing(self):
        db = _FakeSession()
        assert db_cache_get(db, "nope", TTL) is None
        assert db_cache_get_stale(db, "nope") is None
        assert oldest_cache_hit(db) is None

    def test_tolerates_non_session(self):
        # CLI callers pass None; tests pass mocks. Neither may raise.
        assert oldest_cache_hit(None) is None
        assert oldest_cache_hit(object()) is None
