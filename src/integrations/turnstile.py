"""Cloudflare Turnstile: check the token a public form's widget produced.

Off while ``turnstile_secret_key`` is empty, so local dev and tests need no
Cloudflare account. When on, anything short of a clear "yes" from Cloudflare
(missing token, refusal, wrong action, Cloudflare unreachable) is a "no": the
form exists to stop scripts taking Paul's time, so it fails closed.
"""

from __future__ import annotations

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT = 10.0


def turnstile_enabled() -> bool:
    return bool(settings.turnstile_secret_key)


def verify_turnstile(token: str | None, remote_ip: str | None, *, action: str) -> bool:
    """True when the check is off, or Cloudflare confirms ``token`` for ``action``."""
    if not turnstile_enabled():
        return True
    if not token:
        return False
    data = {"secret": settings.turnstile_secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        resp = httpx.post(_VERIFY_URL, data=data, timeout=_TIMEOUT)
        body = resp.json() if resp.is_success else {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile verify failed: %s", exc.__class__.__name__)
        return False
    if not body.get("success"):
        logger.info("Turnstile refused: %s", body.get("error-codes"))
        return False
    # The token must come from this form's widget, not another one on the site.
    # Real tokens always carry ``action``; Cloudflare's test keys (dev) omit it.
    return body.get("action", action) == action
