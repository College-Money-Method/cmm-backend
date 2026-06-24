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
