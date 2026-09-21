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

from fastapi import Request

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


def client_ip(request: Request) -> str:
    """The caller's address, as far as it can be trusted for a rate-limit key.

    The API is reached only through the load balancer, which *appends* the
    address it saw to ``X-Forwarded-For``. So the rightmost entry is the one the
    balancer observed; anything to its left was supplied by the caller and is
    free to invent. Keying on the leftmost entry would hand any bot an unlimited
    supply of fresh buckets simply by rotating a forged header.

    Add a hop (CloudFront or a WAF in front of the balancer) and this has to
    count back that many entries from the right instead.
    """
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    trusted = [part for part in forwarded if part]
    if trusted:
        return trusted[-1]
    return request.client.host if request.client else "unknown"
