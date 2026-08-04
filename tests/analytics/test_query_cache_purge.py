"""Opportunistic TTL cleanup — the sweep that keeps analytics_query_cache bounded.

db_cache_set purges rows no request has refreshed in _MAX_AGE, on a small random
fraction of writes, so the durable cache table can't grow forever without any
scheduler or cron. The sweep is best-effort and must never fail a cache write.
"""

from datetime import datetime, timedelta, timezone

from src.analytics import query_cache
from src.analytics.query_cache import (
    _MAX_AGE,
    AnalyticsQueryCache,
    _purge_stale,
    db_cache_set,
)


class _FakeSession:
    """Records execute/commit/rollback so we can assert what the sweep did.

    Supports the surface db_cache_set + _purge_stale touch: get/add/commit/
    rollback/execute. `execute_raises` simulates a DB error mid-sweep.
    """

    def __init__(self, *rows: AnalyticsQueryCache, execute_raises: bool = False):
        self._rows = {r.key: r for r in rows}
        self.info: dict = {}
        self.added: list = []
        self.executed: list = []          # list[(sql, params)]
        self.commits = 0
        self.rollbacks = 0
        self._execute_raises = execute_raises

    def get(self, _model, key, **_kwargs):
        return self._rows.get(key)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        if self._execute_raises:
            raise RuntimeError("boom")
        return _FakeResult(rowcount=3)


class _FakeResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class TestPurgeStale:
    def test_deletes_rows_past_max_age_and_commits(self):
        db = _FakeSession()
        _purge_stale(db)
        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert "DELETE FROM analytics_query_cache" in sql
        # Cutoff is ~_MAX_AGE ago; anything older gets swept.
        cutoff = params["cutoff"]
        expected = datetime.now(timezone.utc) - _MAX_AGE
        assert abs((cutoff - expected).total_seconds()) < 5
        assert db.commits == 1
        assert db.rollbacks == 0

    def test_swallows_db_errors(self):
        db = _FakeSession(execute_raises=True)
        _purge_stale(db)  # must not raise
        assert db.rollbacks == 1
        assert db.commits == 0


class TestOpportunisticCleanup:
    def test_write_triggers_purge_when_sampled(self, monkeypatch):
        # Force the sample below the threshold → sweep runs after the upsert.
        monkeypatch.setattr(query_cache.random, "random", lambda: 0.0)
        db = _FakeSession()
        db_cache_set(db, "k", {"v": 1})
        assert db.added and db.added[0].key == "k"      # entry written
        assert any("DELETE FROM analytics_query_cache" in sql for sql, _ in db.executed)

    def test_write_skips_purge_when_not_sampled(self, monkeypatch):
        # Sample above the threshold → only the upsert happens, no sweep.
        monkeypatch.setattr(query_cache.random, "random", lambda: 0.99)
        db = _FakeSession()
        db_cache_set(db, "k", {"v": 1})
        assert db.executed == []

    def test_purge_failure_does_not_break_the_write(self, monkeypatch):
        monkeypatch.setattr(query_cache.random, "random", lambda: 0.0)
        db = _FakeSession(execute_raises=True)
        db_cache_set(db, "k", {"v": 1})  # must not raise despite sweep failing
        assert db.added and db.added[0].key == "k"
