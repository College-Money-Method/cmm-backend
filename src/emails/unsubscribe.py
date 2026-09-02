"""Signed tokens for the public, no-login CAN-SPAM unsubscribe link.

`itsdangerous` isn't an existing dependency, so this uses stdlib `hmac`/`hashlib`
over the app's existing secret (`settings.unsubscribe_secret_key`, falling back to
the Supabase service role key) instead of adding a new package for one endpoint.

Token = base64url(f"{contact_ids}:{expiry_epoch}:{hmac_signature}"), where
`contact_ids` is one uuid or several joined by ","; the signature covers
`contact_ids:expiry_epoch`, so neither can be tampered with independently.

Several ids exist for grouped sends (one email addressed to every counselor at a
school): each recipient sees the same footer link, so the token has to cover all
of them or whoever clicks could only unsubscribe a colleague. A single-id token
is just the one-element case, so tokens minted before grouped sends existed keep
verifying unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from src.config import settings
from src.emails.school_links import email_origin

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


def generate_unsubscribe_token(
    contact_id: uuid.UUID | list[uuid.UUID], ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> str:
    """Build a signed, expiring token encoding one or more `contact_id`s for an
    email footer link."""
    ids = contact_id if isinstance(contact_id, list) else [contact_id]
    expiry = int(time.time()) + ttl_seconds
    payload = f"{','.join(str(cid) for cid in ids)}:{expiry}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def verify_unsubscribe_token_ids(token: str) -> list[uuid.UUID]:
    """Verify a token and return every encoded contact_id, or `[]` if it is
    malformed, forged, or expired. Never raises — every failure mode collapses to
    an empty list so the caller can reject with a single generic error and no
    signal about *why*.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        contact_ids_str, expiry_str, signature = raw.split(":", 2)
    except Exception:
        # Any decode/format failure (bad base64, wrong field count, non-utf8) is
        # just an invalid token — collapse to empty rather than propagate.
        return []

    expected_signature = _sign(f"{contact_ids_str}:{expiry_str}")
    if not hmac.compare_digest(expected_signature, signature):
        return []

    try:
        expiry = int(expiry_str)
        contact_ids = [uuid.UUID(part) for part in contact_ids_str.split(",") if part]
    except ValueError:
        return []

    if time.time() > expiry:
        return []

    return contact_ids


def verify_unsubscribe_token(token: str) -> uuid.UUID | None:
    """Single-contact view of `verify_unsubscribe_token_ids` — the first encoded
    contact_id, or None when the token is invalid."""
    ids = verify_unsubscribe_token_ids(token)
    return ids[0] if ids else None


def email_preferences_url(token: str) -> str:
    """Absolute link to the frontend preference page for `token`, e.g.
    ``https://next.collegemoneymethod.com/email-preferences?token=...``.

    The page is a public frontend route, not an API endpoint: recipients (and
    their mail clients, which POST here for RFC 8058 one-click) should only ever
    see the site's own origin.
    """
    return f"{email_origin()}/email-preferences?token={token}"


def build_unsubscribe_url(contact_id: uuid.UUID | list[uuid.UUID]) -> str:
    """Absolute unsubscribe/preference link for an email footer and its
    ``List-Unsubscribe`` header."""
    return email_preferences_url(generate_unsubscribe_token(contact_id))
