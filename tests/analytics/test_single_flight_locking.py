"""single_flight must use a TRANSACTION-scoped advisory lock.

Regression guard for a hang that took down the analytics tabs: with the
session-level pg_advisory_lock, db_cache_set()'s mid-computation db.commit()
returned the connection to the pool, so the paired pg_advisory_unlock ran on a
different pooled connection (7 of 8 times under concurrency), silently returned
false, and left the lock held by an idle connection forever. Every later request
for that cache key then blocked until the frontend's 30s stream timeout.

pg_advisory_xact_lock cannot leak: Postgres releases it at transaction end.
"""

import pytest
from sqlalchemy.exc import OperationalError

from src.analytics.query_cache import single_flight


class _RecordingSession:
    """Captures SQL instead of running it; can fail the lock acquisition."""

    def __init__(self, fail_lock: bool = False):
        self.sql: list[str] = []
        self.rollbacks = 0
        self._fail_lock = fail_lock
        self.info: dict = {}

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if self._fail_lock and "pg_advisory_xact_lock" in sql:
            raise OperationalError("SELECT pg_advisory_xact_lock", params, Exception("timeout"))
        return None

    def rollback(self):
        self.rollbacks += 1

    def joined(self) -> str:
        return " | ".join(self.sql)


class TestSingleFlightLocking:
    def test_takes_transaction_scoped_lock(self):
        db = _RecordingSession()
        with single_flight(db, "some:key"):
            ran = True
        assert ran
        assert "pg_advisory_xact_lock" in db.joined()

    def test_never_takes_the_session_level_lock(self):
        """The session-level variant is the leak — it must not reappear."""
        db = _RecordingSession()
        with single_flight(db, "some:key"):
            pass
        assert "pg_advisory_lock(" not in db.joined()

    def test_never_unlocks_by_hand(self):
        """An explicit unlock is the bug's other half: it ran on whatever
        connection the pool handed back. The transaction end is the release."""
        db = _RecordingSession()
        with single_flight(db, "some:key"):
            pass
        assert "pg_advisory_unlock" not in db.joined()

    def test_bounds_the_wait(self):
        db = _RecordingSession()
        with single_flight(db, "some:key"):
            pass
        assert "lock_timeout" in db.joined()

    def test_lock_failure_degrades_to_uncoalesced_compute(self):
        """A timeout must not fail the request — duplicate PostHog work beats a
        hung page. The aborted transaction is rolled back first so the caller's
        own queries still run."""
        db = _RecordingSession(fail_lock=True)
        ran = False
        with single_flight(db, "some:key"):
            ran = True
        assert ran
        assert db.rollbacks == 1

    def test_no_db_is_a_noop(self):
        ran = False
        with single_flight(None, "some:key"):
            ran = True
        assert ran

    def test_body_exception_propagates(self):
        """Nothing to clean up, but the caller's error must not be swallowed."""
        db = _RecordingSession()
        with pytest.raises(ValueError):
            with single_flight(db, "some:key"):
                raise ValueError("compute failed")
