"""School Resource Center session tokens and the gate built on them.

A visitor proves they know a school's portal password (or the school has none)
and receives a signed token, sent back as ``X-SRC-Session``. The token is only a
receipt: the school's access mode is recomputed from the database on every
request, so converting a prospect to a customer unlocks it immediately.

Counselor Hub callers send a Supabase bearer token instead; a staff bearer is
accepted wherever a session is, always in full mode.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.config import settings
from src.db.client import get_supabase
from src.db.deps import DbDep, get_db
from src.schools.models import School

logger = logging.getLogger(__name__)

Mode = Literal["full", "preview"]

SESSION_TTL = timedelta(days=30)
_ALGORITHM = "HS256"
_DEV_SECRET = "insecure-dev-src-session-secret"
_warned_dev_secret = False
_LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})


@dataclass(frozen=True)
class SrcSession:
    school: School
    mode: Mode


def src_error(status_code: int, code: str, message: str) -> HTTPException:
    """HTTP error whose ``detail`` is ``{code, message}``."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _uses_dev_secret_fallback() -> bool:
    """Only local runs and tests may sign with the hardcoded secret; deployed
    environments (dev, prod) must provide SRC_SESSION_SECRET."""
    return settings.environment.lower() in _LOCAL_ENVIRONMENTS


def _secret() -> str:
    global _warned_dev_secret
    if settings.src_session_secret:
        return settings.src_session_secret
    if not _uses_dev_secret_fallback():
        # Fail closed: a publicly known signing key would let anyone forge a
        # full-access session for any school.
        raise RuntimeError("SRC_SESSION_SECRET is not set")
    if not _warned_dev_secret:
        logger.warning("SRC_SESSION_SECRET is not set; using an insecure development secret")
        _warned_dev_secret = True
    return _DEV_SECRET


def check_src_session_secret() -> None:
    """Startup check: log loudly when a deployed environment has no secret.
    Logs instead of raising so the rest of the API stays up; SRC sessions fail
    closed until the secret is set."""
    if not settings.src_session_secret and not _uses_dev_secret_fallback():
        logger.error(
            "SRC_SESSION_SECRET is not set in environment %r; School Resource Center "
            "sessions will be refused until it is configured",
            settings.environment,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def school_mode(school: School) -> Mode:
    return "preview" if school.is_src_preview and not school.is_current_customer else "full"


def preview_expiry(school: School) -> datetime | None:
    return _aware(school.src_preview_expires_at) if school_mode(school) == "preview" else None


def is_preview_expired(school: School) -> bool:
    expiry = preview_expiry(school)
    return expiry is not None and expiry <= _now()


def ensure_preview_active(school: School) -> None:
    if is_preview_expired(school):
        raise src_error(403, "preview_expired", "This preview has expired. Contact College Money Method to continue.")


def password_version(password: str | None) -> str:
    """Short keyed digest of the password; changing the password voids old tokens."""
    return hmac.new(_secret().encode(), (password or "").encode(), hashlib.sha256).hexdigest()[:16]


def issue_token(school: School) -> tuple[str, Mode, datetime | None]:
    """Return (token, mode, preview_expires_at) for a school."""
    mode = school_mode(school)
    expiry = preview_expiry(school)
    exp = _now() + SESSION_TTL
    if expiry is not None:
        exp = min(exp, expiry)
    claims = {
        "sub": str(school.id),
        "mode": mode,
        "pwv": password_version(school.cmm_website_password),
        "exp": exp,
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM), mode, expiry


def _decode(token: str) -> dict:
    try:
        claims = jwt.decode(token, _secret(), algorithms=[_ALGORITHM], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise src_error(401, "src_session_invalid", "Your session is invalid or has expired.") from exc
    return claims


def _check_token(token: str, school: School) -> None:
    claims = _decode(token)
    try:
        subject = uuid.UUID(str(claims.get("sub")))
    except ValueError as exc:
        raise src_error(401, "src_session_invalid", "Your session is invalid or has expired.") from exc
    if subject != school.id:
        raise src_error(403, "src_session_wrong_school", "This session belongs to a different school.")
    if not hmac.compare_digest(str(claims.get("pwv", "")), password_version(school.cmm_website_password)):
        raise src_error(401, "src_session_invalid", "Your session is invalid or has expired.")


def is_staff_for(user: CurrentUser | None, school_id: uuid.UUID | None) -> bool:
    """Admins see everything; hub users see their own school."""
    if user is None:
        return False
    if user.role == "super_admin":
        return True
    return school_id is not None and user.role in ("hub_admin", "hub_user") and user.school_id == school_id


def is_any_staff(user: CurrentUser | None) -> bool:
    """Admin or hub user; for school-less reads of already-published content."""
    return user is not None and user.role in ("super_admin", "hub_admin", "hub_user")


def authorize(school: School, token: str | None, user: CurrentUser | None = None) -> SrcSession:
    """Decide whether a request may use ``school``'s gated content.

    A present-but-bad token is always rejected. A missing one is tolerated only
    for full-access schools while enforcement is off.
    """
    if is_staff_for(user, school.id):
        return SrcSession(school, "full")
    ensure_preview_active(school)
    mode = school_mode(school)
    if token:
        _check_token(token, school)
    elif mode == "preview" or settings.src_session_enforced:
        raise src_error(401, "src_session_required", "Please sign in to the resource center.")
    else:
        logger.info("src session missing for school %s", school.id)
    return SrcSession(school, mode)


# ── FastAPI dependencies ──────────────────────────────────────────────────────

SrcSessionHeader = Annotated[str | None, Header(alias="X-SRC-Session")]
_bearer = HTTPBearer(auto_error=False)


async def optional_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db=Depends(get_db),
    supabase=Depends(get_supabase),
) -> CurrentUser | None:
    """The bearer's user, or None when absent or unusable. Never raises."""
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials, db, supabase)
    except HTTPException:
        return None


OptionalUserDep = Annotated[CurrentUser | None, Depends(optional_current_user)]


def session_for_school(
    db, school_id: uuid.UUID, token: str | None, user: CurrentUser | None
) -> SrcSession:
    """Authorize against an explicitly requested school id."""
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    return authorize(school, token, user)


def require_admin_without_school(user: CurrentUser | None) -> None:
    """Gate for school-less calls that would expose full content: admins only."""
    if user is None or user.role != "super_admin":
        raise src_error(401, "src_session_required", "Please sign in to the resource center.")


def token_school_mode(db: DbDep, token: str | None) -> SrcSession | None:
    """Best-effort session for optional shaping (search). Invalid tokens yield None."""
    if not token:
        return None
    try:
        claims = _decode(token)
        school = db.get(School, uuid.UUID(str(claims.get("sub"))))
        if school is None:
            return None
        _check_token(token, school)
        return SrcSession(school, school_mode(school))
    except (HTTPException, ValueError):
        return None
