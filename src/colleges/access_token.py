"""Access token for the College Aid Explorer and the dependency that checks it.

The JWT carries ``aud="college-aid"`` and verification requires it, so SRC
school tokens (same signing secret, no audience) are never accepted here and
vice versa. ``pwv`` is a keyed digest of the stored password hash: rotating or
clearing the password voids every outstanding token on the next request.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select

from src.app_config.models import AppConfig
from src.db.deps import DbDep
from src.schools.src_session import OptionalUserDep, signing_secret

AUDIENCE = "college-aid"
TOKEN_TTL = timedelta(days=7)
TOKEN_HEADER = "X-College-Aid-Token"
_ALGORITHM = "HS256"


def current_password_hash(db) -> str | None:
    """The stored password hash, or None when the tool is closed."""
    return db.scalar(select(AppConfig.college_aid_password_hash).limit(1))


def password_version(stored_hash: str | None) -> str:
    """Short keyed digest of the stored hash; a new password changes it."""
    return hmac.new(signing_secret().encode(), (stored_hash or "").encode(), hashlib.sha256).hexdigest()[:16]


def issue_access_token(stored_hash: str) -> tuple[str, datetime]:
    """Return (token, expires_at) bound to the current password."""
    expires_at = datetime.now(UTC) + TOKEN_TTL
    claims = {
        "sub": AUDIENCE,
        "aud": AUDIENCE,
        "pwv": password_version(stored_hash),
        "exp": expires_at,
    }
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


def require_college_aid_access(
    db: DbDep,
    user: OptionalUserDep,
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> None:
    """Allow a valid access token, or a super_admin bearer (admin preview)."""
    if user is not None and user.role == "super_admin":
        return
    if not token:
        raise HTTPException(status_code=401, detail="college_aid_token_required")
    if not verify_access_token(token, current_password_hash(db)):
        raise HTTPException(status_code=401, detail="college_aid_token_invalid")


CollegeAidAccessDep = Depends(require_college_aid_access)
