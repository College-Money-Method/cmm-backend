"""HMAC signing shared by the app's public, no-login token links.

One secret and one signature scheme for every emailed link that has to prove
itself without a session: the unsubscribe link and Paul's booking-review link.
Each caller owns its own payload format and expiry; only the signing lives here.
"""

from __future__ import annotations

import hashlib
import hmac

from src.config import settings


def secret() -> bytes:
    key = settings.unsubscribe_secret_key or settings.supabase_service_role_key
    if not key:
        raise RuntimeError(
            "No secret configured for signed link tokens "
            "(set UNSUBSCRIBE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY)"
        )
    return key.encode("utf-8")


def sign(payload: str) -> str:
    return hmac.new(secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
