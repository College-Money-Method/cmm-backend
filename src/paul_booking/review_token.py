"""Signed, expiring token behind the review link in Paul's email.

``base64url("{request_id}:{expires_epoch}:{hmac}")`` signed with the same secret
and scheme as the unsubscribe link (``src.utils.signed_token``). The token proves
who is asking; the database still decides what state the request is in. Its
expiry is the request's ``expires_at``, so the link dies when the hold lapses.
"""

from __future__ import annotations

import base64
import hmac
import uuid
from datetime import datetime

from src.paul_booking.rules import as_aware
from src.utils import signed_token


def expiry_epoch(expires_at: datetime) -> int:
    return int(as_aware(expires_at).timestamp())


def make_review_token(request_id: uuid.UUID, expires_at: datetime) -> str:
    payload = f"{request_id}:{expiry_epoch(expires_at)}"
    raw = f"{payload}:{signed_token.sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def parse_review_token(token: str) -> tuple[uuid.UUID, int] | None:
    """``(request_id, expires_epoch)`` for an authentic token, else None.

    Checks the signature only; the caller compares the epoch with the stored
    ``expires_at`` and with the clock. Never raises, so every malformed or forged
    token collapses to one generic answer.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        id_str, epoch_str, signature = raw.split(":", 2)
        if not hmac.compare_digest(signed_token.sign(f"{id_str}:{epoch_str}"), signature):
            return None
        return uuid.UUID(id_str), int(epoch_str)
    except Exception:
        return None
