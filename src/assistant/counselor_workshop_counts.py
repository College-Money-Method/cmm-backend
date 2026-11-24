"""Registration and attendance figures for the counselor's workshop list.

A webinar serves the schools or cohorts it is allocated to. Each figure is that
workshop's total and says so in its own words ("total registrations for this
workshop"), so it is never read as the school's own count; no school's own share is
given, the counselor's included. Only counts leave this module, never who registered. Counted the way the
Hub counts (every registration row, whatever its status), in one grouped query for
the whole list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from src.workshops.models import WorkshopRegistration


TOTAL_REGISTERED = "total registrations for this workshop"


@dataclass(frozen=True)
class WorkshopCounts:
    registered: int = 0
    attended: int = 0

    def describe(self, held: bool) -> str:
        text = f"{TOTAL_REGISTERED}: {self.registered}"
        if held:
            text += f"; total attended: {self.attended}"
        return text


def workshop_counts(db: Session, webinar_ids: list[uuid.UUID]) -> dict[uuid.UUID, WorkshopCounts]:
    """``{webinar_id: WorkshopCounts}``; a webinar with no registrations counts zeros."""
    if not webinar_ids:
        return {}
    reg = WorkshopRegistration
    rows = db.execute(
        select(
            reg.webinar_id,
            func.count(reg.id),
            func.coalesce(func.sum(case((reg.attended.is_(True), 1), else_=0)), 0),
        )
        .where(reg.webinar_id.in_(webinar_ids))
        .group_by(reg.webinar_id)
    ).all()
    found = {wid: WorkshopCounts(int(registered), int(attended)) for wid, registered, attended in rows}
    return {wid: found.get(wid, WorkshopCounts()) for wid in webinar_ids}
