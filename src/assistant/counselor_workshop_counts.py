"""Registration and attendance figures for the counselor's workshop list.

A webinar is often shared by several host schools, so each figure comes as a
pair: the session's total across every host school, and the asking counselor's
school's share of it. Only counts leave this module, never who registered, and
other schools are never broken out. Counted the way the Hub counts (every
registration row, whatever its status), in one grouped query for the whole list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from src.workshops.models import WorkshopRegistration


@dataclass(frozen=True)
class WorkshopCounts:
    registered_total: int = 0
    registered_school: int = 0
    attended_total: int = 0
    attended_school: int = 0

    def describe(self, held: bool) -> str:
        text = f"registered: {self.registered_total} total, {self.registered_school} from your school"
        if held:
            text += f"; attended: {self.attended_total} total, {self.attended_school} from your school"
        return text


def workshop_counts(db: Session, webinar_ids: list[uuid.UUID], school_id: uuid.UUID) -> dict[uuid.UUID, WorkshopCounts]:
    """``{webinar_id: WorkshopCounts}``; a webinar with no registrations counts zeros."""
    if not webinar_ids:
        return {}
    reg = WorkshopRegistration
    ours = reg.school_id == school_id

    def tally(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    rows = db.execute(
        select(
            reg.webinar_id,
            func.count(reg.id),
            tally(ours),
            tally(reg.attended.is_(True)),
            tally(ours & reg.attended.is_(True)),
        )
        .where(reg.webinar_id.in_(webinar_ids))
        .group_by(reg.webinar_id)
    ).all()
    found = {wid: WorkshopCounts(*(int(n) for n in counts)) for wid, *counts in rows}
    return {wid: found.get(wid, WorkshopCounts()) for wid in webinar_ids}
