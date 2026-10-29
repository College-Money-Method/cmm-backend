"""Access to the AI assistant: shared password -> token, plus the anonymous user id.

Same rules as the College Aid Explorer token (``src.colleges.access_token``):
``aud="assistant"`` keeps college-aid and SRC tokens (same signing secret) out,
and ``pwv`` binds a token to the current password, so rotating or clearing it
voids every issued token on the next request.

The device id comes from the frontend's cookie as ``X-Assistant-User-Id``. It
is a random UUID with no account behind it — it scopes chat history to one
browser and carries no personal data.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select

from src.app_config.models import AppConfig
from src.db.deps import DbDep
from src.schools.src_session import signing_secret

AUDIENCE = "assistant"
TOKEN_TTL = timedelta(days=7)
TOKEN_HEADER = "X-Assistant-Token"
USER_ID_HEADER = "X-Assistant-User-Id"
_ALGORITHM = "HS256"


def current_password_hash(db) -> str | None:
    """The stored password hash, or None when the assistant is closed."""
    return db.scalar(select(AppConfig.assistant_password_hash).limit(1))


def password_version(stored_hash: str | None) -> str:
    """Short keyed digest of the stored hash; a new password changes it."""
    return hmac.new(signing_secret().encode(), (stored_hash or "").encode(), hashlib.sha256).hexdigest()[:16]


def issue_access_token(stored_hash: str) -> tuple[str, datetime]:
    """Return (token, expires_at) bound to the current password."""
    expires_at = datetime.now(UTC) + TOKEN_TTL
    claims = {"sub": AUDIENCE, "aud": AUDIENCE, "pwv": password_version(stored_hash), "exp": expires_at}
    return jwt.encode(claims, signing_secret(), algorithm=_ALGORITHM), expires_at


def verify_access_token(token: str, stored_hash: str | None) -> bool:
    """True only for an unexpired token for this audience and password version."""
    if not stored_hash:
        return False
    try:
        claims = jwt.decode(
            token,
            signing_secret(),
            algorithms=[_ALGORITHM],
            audience=AUDIENCE,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.PyJWTError:
        return False
    return hmac.compare_digest(str(claims.get("pwv", "")), password_version(stored_hash))


def require_assistant_user(
    db: DbDep,
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    user_id: Annotated[str | None, Header(alias=USER_ID_HEADER)] = None,
) -> uuid.UUID:
    """Check the access token and return the anonymous device id it travels with."""
    if not token:
        raise HTTPException(status_code=401, detail="assistant_token_required")
    if not verify_access_token(token, current_password_hash(db)):
        raise HTTPException(status_code=401, detail="assistant_token_invalid")
    try:
        return uuid.UUID(user_id or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="assistant_user_id_invalid") from None


AssistantUserDep = Annotated[uuid.UUID, Depends(require_assistant_user)]
