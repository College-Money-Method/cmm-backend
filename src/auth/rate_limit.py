"""Minimal in-memory sliding-window rate limiter.

Per-process only (NOT shared across uvicorn workers), so treat it as a speed
bump rather than a hard guarantee. It exists to blunt automated abuse of the
public email-existence check, which by design leaks account existence
(user-enumeration). For a strict cross-worker limit, back this with Redis or put
a CAPTCHA in front of the endpoint.
"""

import time
from collections import defaultdict, deque
from threading import Lock

_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def allow(key: str, limit: int, window_seconds: float) -> bool:
    """Return True if ``key`` is under ``limit`` hits in the trailing window.

    Records the hit when allowed. Uses a monotonic clock so it is immune to wall
    clock adjustments.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        q = _hits[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True
