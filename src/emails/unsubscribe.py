"""Signed tokens for the public, no-login CAN-SPAM unsubscribe link.

`itsdangerous` isn't an existing dependency, so this uses stdlib `hmac`/`hashlib`
over the app's existing secret (`settings.unsubscribe_secret_key`, falling back to
the Supabase service role key) instead of adding a new package for one endpoint.

Token = base64url(f"{contact_id}:{expiry_epoch}:{hmac_signature}"). The signature
covers `contact_id:expiry_epoch`, so neither can be tampered with independently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from src.config import settings

# Unsubscribe links live in emails that may sit unread for a long time — a
# short TTL would make old emails' links silently stop working.
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 365  # 1 year


def _secret() -> bytes:
    secret = settings.unsubscribe_secret_key or settings.supabase_service_role_key
    if not secret:
        raise RuntimeError(
            "No secret configured for unsubscribe token signing "
            "(set UNSUBSCRIBE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY)"
        )
    return secret.encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_unsubscribe_token(contact_id: uuid.UUID, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Build a signed, expiring token encoding `contact_id` for an email footer link."""
    expiry = int(time.time()) + ttl_seconds
    payload = f"{contact_id}:{expiry}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def verify_unsubscribe_token(token: str) -> uuid.UUID | None:
    """Verify a token and return the encoded contact_id, or None if it is malformed,
    forged, or expired. Never raises — every failure mode collapses to None so the
    caller can reject with a single generic error and no signal about *why*.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        contact_id_str, expiry_str, signature = raw.split(":", 2)
    except Exception:
        # Any decode/format failure (bad base64, wrong field count, non-utf8) is
        # just an invalid token — collapse to None rather than propagate.
        return None

    expected_signature = _sign(f"{contact_id_str}:{expiry_str}")
    if not hmac.compare_digest(expected_signature, signature):
        return None

    try:
        expiry = int(expiry_str)
        contact_id = uuid.UUID(contact_id_str)
    except ValueError:
        return None

    if time.time() > expiry:
        return None

    return contact_id


def build_unsubscribe_url(contact_id: uuid.UUID) -> str:
    """Absolute unsubscribe link for an email footer, e.g.
    ``https://api.collegemoneymethod.com/api/v1/emails/unsubscribe?token=...``.
    """
    token = generate_unsubscribe_token(contact_id)
    base = settings.app_public_url.rstrip("/")
    return f"{base}/api/v1/emails/unsubscribe?token={token}"
