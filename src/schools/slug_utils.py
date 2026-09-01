"""Slug generation utilities for School records."""
from __future__ import annotations

import re
import unicodedata
import uuid


def slugify(text: str) -> str:
    """Convert a school name to a URL-safe slug.

    "Springfield High School" → "springfield-high-school"
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def unique_slug(base: str, existing: set[str]) -> str:
    """Return a slug derived from *base* that is not in *existing*.

    Appends -2, -3, … until a free slot is found:
      "Springfield High" + {"springfield-high"} → "springfield-high-2"
    """
    slug = slugify(base)
    if slug not in existing:
        return slug
    counter = 2
    while f"{slug}-{counter}" in existing:
        counter += 1
    return f"{slug}-{counter}"


def unique_slug_db(base: str, db: object, exclude_id: uuid.UUID | None = None) -> str:
    """Generate a unique slug by checking against the schools table.

    Accepts a SQLAlchemy Session for on-demand DB lookups (used by the
    create/update endpoints where the full slug set is not pre-loaded).
    """
    from sqlalchemy.orm import Session

    from src.schools.models import School

    session: Session = db  # type: ignore[assignment]
    slug = slugify(base)
    counter = 2
    candidate = slug
    while True:
        q = session.query(School.id).filter(School.slug == candidate)
        if exclude_id:
            q = q.filter(School.id != exclude_id)
        if not q.first():
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


# Admin-facing slug rules. Kept deliberately narrow: the slug becomes a public
# URL segment (/school/<slug>), so only lowercase alphanumerics and single
# hyphens are allowed.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Reserved because /school/<slug>/<these> are real portal sub-routes — a school
# slug colliding with one would make its own URLs ambiguous.
RESERVED_SLUGS = frozenset(
    {"admin", "api", "hub", "school", "schools", "topic", "resources", "workshops", "static"}
)


def validate_custom_slug(value: str) -> str:
    """Normalize and validate an admin-supplied slug.

    Returns the cleaned slug. Raises ValueError with a user-facing message when
    the value can't be used as a public URL segment.
    """
    slug = slugify(value.strip())
    if not slug:
        raise ValueError("Slug must contain at least one letter or number.")
    if len(slug) > 100:
        raise ValueError("Slug must be 100 characters or fewer.")
    if not _SLUG_RE.match(slug):
        raise ValueError("Slug may only contain lowercase letters, numbers and single hyphens.")
    if slug in RESERVED_SLUGS:
        raise ValueError(f'"{slug}" is reserved and can\'t be used as a school slug.')
    return slug


def find_slug_owner(slug: str, db: object, exclude_id: uuid.UUID | None = None):
    """Return the School already using *slug*, or None when it's free.

    Checks ``airtable_slug`` as well as ``slug`` because /school/<slug> still
    resolves legacy Airtable slugs (see ``_find_public_school``) — reusing one
    would make the URL ambiguous.
    """
    from sqlalchemy import or_
    from sqlalchemy.orm import Session

    from src.schools.models import School

    session: Session = db  # type: ignore[assignment]
    q = session.query(School).filter(or_(School.slug == slug, School.airtable_slug == slug))
    if exclude_id:
        q = q.filter(School.id != exclude_id)
    return q.first()
