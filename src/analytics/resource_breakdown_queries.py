"""Resource-usage rankings keyed by asset ID rather than asset name.

`resource_viewed` carries both `asset_id` and `asset_name`, and these lists used
to group by the NAME. That was wrong in both directions:

  • two assets sharing a title merged into one row (measured on live data:
    40 distinct asset ids behind only 30 distinct names), inflating the winner
    and hiding the other;
  • renaming an asset split its history into a before-row and an after-row.

Grouping by id fixes both, and names come from Postgres — so the list always
shows the CURRENT name, retroactively, and the row can link to the resource page.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.analytics.schemas import RankedContentRow, TopBreakdown
from src.content.models import ContentAsset
from src.schools.models import School

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def get_school_slug(db: Session, school_id: str | None) -> str | None:
    """Slug for building resource links, or None (no school → unlinked rows)."""
    if not school_id or not _UUID_RE.match(school_id):
        return None
    return db.execute(
        select(School.slug).where(School.id == school_id)
    ).scalar_one_or_none()


def resolve_asset_rows(db: Session, rows: list[TopBreakdown]) -> list[RankedContentRow]:
    """Turn id-labelled breakdown rows into named, linkable rows.

    Input `label` is an asset id (that's the breakdown property). Order is
    preserved — the caller already ranked by count.

    An id with no matching row is kept, NOT dropped: the views really happened,
    and silently losing them would make the totals tile disagree with the list.
    It renders unlinked (the page would 404) and is labelled as removed, with a
    short id fragment so several removed assets stay distinguishable.
    """
    ids = {r.label for r in rows if _UUID_RE.match(r.label)}
    names: dict[str, str] = {}
    if ids:
        found = db.execute(
            select(ContentAsset.id, ContentAsset.name).where(
                ContentAsset.id.in_([uuid.UUID(i) for i in ids])
            )
        ).all()
        names = {str(aid): name for aid, name in found}

    out: list[RankedContentRow] = []
    for r in rows:
        name = names.get(r.label)
        if name is None:
            out.append(RankedContentRow(
                id=None,
                name=f"Removed resource ({r.label[:8]})",
                count=int(r.count),
            ))
        else:
            out.append(RankedContentRow(id=r.label, name=name, count=int(r.count)))
    return out
