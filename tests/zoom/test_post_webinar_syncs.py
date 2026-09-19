"""The retry ladder behind ``webinar.ended``.

Both post-webinar reports come from the same Zoom Reports API and share its
5-30 minute availability lag, so they share one ladder. What must hold is that
they stay independent inside it: a report that arrives early stops being asked
for, and one that never arrives cannot hold back or roll back the other.
"""

from __future__ import annotations

import asyncio

import pytest

from src.zoom import webhook_router
from src.zoom.webhook_router import _POST_WEBINAR_SYNCS, _sync_with_retry

WEBINAR_ID = "83822890565"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Skip the 15- and 30-minute sleeps; the ladder's shape is what is tested."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(webhook_router.asyncio, "sleep", _instant)


@pytest.fixture(autouse=True)
def sessions(monkeypatch):
    """A session factory handing out objects that only need to be closeable."""

    class _Session:
        def close(self):
            pass

    monkeypatch.setattr(webhook_router, "get_session_factory", lambda: _Session)


def _ladder(monkeypatch, **behaviours):
    """Install a stub for each named sync and return its call log."""
    calls: dict[str, int] = {name: 0 for name in behaviours}

    def _make(name, outcomes):
        def _sync(zoom_webinar_id, db):
            calls[name] += 1
            outcome = outcomes[min(calls[name] - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return _sync

    monkeypatch.setattr(
        webhook_router,
        "_POST_WEBINAR_SYNCS",
        {name: _make(name, outcomes) for name, outcomes in behaviours.items()},
    )
    return calls


def test_both_reports_are_pulled(monkeypatch):
    # The Q&A report is pulled by the same event as attendance — nothing else
    # fires after a webinar ends.
    assert set(_POST_WEBINAR_SYNCS) == {"attendance", "Q&A"}


def test_a_report_that_arrives_first_time_is_not_asked_for_again(monkeypatch):
    calls = _ladder(monkeypatch, attendance=[True], **{"Q&A": [True]})

    asyncio.run(_sync_with_retry(WEBINAR_ID))

    assert calls == {"attendance": 1, "Q&A": 1}


def test_a_late_report_is_retried_while_the_other_stops(monkeypatch):
    calls = _ladder(monkeypatch, attendance=[True], **{"Q&A": [False, True]})

    asyncio.run(_sync_with_retry(WEBINAR_ID))

    # Attendance landed on the first attempt and drops out of the ladder.
    assert calls == {"attendance": 1, "Q&A": 2}


def test_one_report_raising_does_not_stop_the_other(monkeypatch):
    calls = _ladder(
        monkeypatch,
        attendance=[RuntimeError("Zoom 500")],
        **{"Q&A": [True]},
    )

    # An error inside one sync is caught per-attempt: the Q&A report still gets
    # its turn on the same pass, and the handler never sees the exception.
    asyncio.run(_sync_with_retry(WEBINAR_ID))

    assert calls["Q&A"] == 1
    assert calls["attendance"] == 3


def test_a_report_that_never_arrives_gives_up_after_the_last_rung(monkeypatch):
    calls = _ladder(monkeypatch, attendance=[True], **{"Q&A": [False]})

    asyncio.run(_sync_with_retry(WEBINAR_ID))

    assert calls["Q&A"] == len(webhook_router._RETRY_DELAYS)
