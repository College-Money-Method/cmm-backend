"""Leader election across the uvicorn workers that share a container.

The bug this exists to stop was silent and expensive: with two workers, two
schedulers ran the same sweep at the same instant, and two chaptering passes
raced each other's Vimeo writes until one failed a job whose replay had already
published. So the interesting case is not "a lock can be taken" — it is that a
*second process* is refused while the first holds it.
"""

from __future__ import annotations

import multiprocessing
import os

import pytest

from src.utils import single_writer


@pytest.fixture(autouse=True)
def _release_locks():
    """Leadership is process-wide state; don't leak it between tests."""
    yield
    for fd in single_writer._held.values():
        os.close(fd)
    single_writer._held.clear()


def _try_in_child(name: str, result):
    # Fresh import state per process is what production has: each uvicorn worker
    # starts with an empty `_held`.
    single_writer._held.clear()
    result.value = 1 if single_writer.is_leader(name) else 0


def _ask_child(name: str) -> bool:
    ctx = multiprocessing.get_context("spawn")
    result = ctx.Value("i", -1)
    child = ctx.Process(target=_try_in_child, args=(name, result))
    child.start()
    child.join(30)
    assert result.value in (0, 1), "child did not report a result"
    return bool(result.value)


def test_a_second_process_is_not_the_leader(tmp_path, monkeypatch):
    monkeypatch.setattr(single_writer.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert single_writer.is_leader("sweep-test") is True
    assert _ask_child("sweep-test") is False


def test_leadership_passes_on_once_the_holder_is_gone(tmp_path, monkeypatch):
    """A worker that dies must not take the sweep with it."""
    monkeypatch.setattr(single_writer.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert single_writer.is_leader("handover-test") is True
    os.close(single_writer._held.pop("handover-test"))

    assert _ask_child("handover-test") is True


def test_the_leader_stays_the_leader_on_later_ticks(tmp_path, monkeypatch):
    """Re-asking must be cheap and must not drop the lock."""
    monkeypatch.setattr(single_writer.tempfile, "gettempdir", lambda: str(tmp_path))

    assert single_writer.is_leader("sticky-test") is True
    assert single_writer.is_leader("sticky-test") is True
    assert len(single_writer._held) == 1
