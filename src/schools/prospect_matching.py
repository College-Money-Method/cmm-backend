"""Matching a fit-check submission against schools we already know.

Pure helpers (name normalisation, the generated password) plus the lookup that
decides what a submitted school name means: an existing partner, a preview that
already exists, or a school we may open a preview for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from src.schools.models import School
from src.schools.src_session import is_preview_expired, preview_expiry

ProspectStatus = Literal["available", "existing_partner", "preview_exists", "preview_expired"]

# Words skipped when building the initials password
_STOP_WORDS = frozenset({"the", "of", "and", "a", "an", "at", "for"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_school_name(name: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    return _NON_ALNUM.sub(" ", name.lower()).strip()


def initials_password(school_name: str) -> str:
    """Initials of the significant words plus ``cmm``: "Baylor School" -> "bscmm"."""
    words = [w for w in _NON_ALNUM.sub(" ", school_name.lower()).split() if w not in _STOP_WORDS]
    return "".join(w[0] for w in words) + "cmm"


@dataclass(frozen=True)
class ProspectMatch:
    status: ProspectStatus
    school: School | None = None
    expires_at: datetime | None = None

    @property
    def slug(self) -> str | None:
        return self.school.slug if self.school is not None and self.status != "available" else None


def classify(school: School) -> ProspectMatch:
    """What an existing school means for a prospect asking about it."""
    if school.is_current_customer:
        return ProspectMatch("existing_partner", school)
    if school.is_src_preview:
        status: ProspectStatus = "preview_expired" if is_preview_expired(school) else "preview_exists"
        return ProspectMatch(status, school, preview_expiry(school))
    if school.is_cmm_website_activated:
        return ProspectMatch("existing_partner", school)
    # A pipeline prospect nobody has activated: do not reveal it exists
    return ProspectMatch("available", school)


# Lower sorts first when several schools share a name and state
_RANK = {"existing_partner": 0, "preview_exists": 1, "preview_expired": 2, "available": 3}


def find_prospect_match(db, name: str, state: str | None) -> ProspectMatch:
    """Match on normalised name plus state (name only when state is empty)."""
    target = normalize_school_name(name)
    if not target:
        return ProspectMatch("available")
    query = select(School.id, School.name)
    if state:
        query = query.where(func.upper(School.state) == state.upper())
    ids = [row.id for row in db.execute(query).all() if normalize_school_name(row.name) == target]
    if not ids:
        return ProspectMatch("available")
    schools = db.query(School).filter(School.id.in_(ids)).all()
    return min((classify(s) for s in schools), key=lambda m: _RANK[m.status])
