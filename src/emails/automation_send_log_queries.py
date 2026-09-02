"""Paginated read model over `email_send_log` for one automation.

Kept out of `automation_router.py` (already at its size budget) because this is
a read model, not CRUD: it joins the workshop context columns the runner writes
(`webinar_id`, `school_id`) back to human-readable school/workshop names.

Cycle scoping goes through `webinars.cycle_id`, matching every other cycle
filter in the app (`analytics/postgres_queries.py`). Filtering on `sent_at`
instead would misfile reminders that fire across a cycle boundary — a
pre-workshop reminder goes out days before the workshop it belongs to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.emails.models import EmailSendLog
from src.schools.models import School
from src.workshops.models import Webinar, Workshop


class AutomationSendRow(BaseModel):
    id: uuid.UUID
    recipient_email: str
    subject: str
    status: str
    sent_at: datetime
    school_name: str | None
    workshop_name: str | None
    webinar_start: datetime | None


class AutomationSendPage(BaseModel):
    rows: list[AutomationSendRow]
    has_more: bool


def automation_sends(
    db: Session,
    automation_id: uuid.UUID,
    *,
    cycle_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = 50,
) -> AutomationSendPage:
    """One page of this automation's send log, newest first.

    `cycle_id` narrows to sends whose webinar belongs to that cycle. Rows with
    no `webinar_id` (logged before that column existed, or unresolvable) are
    excluded by that filter rather than guessed into a cycle — they remain
    reachable with `cycle_id=None`.
    """
    stmt = (
        select(
            EmailSendLog,
            School.name.label("school_name"),
            Workshop.name.label("workshop_name"),
            Webinar.start_datetime.label("webinar_start"),
        )
        # Outer joins throughout: broadcast-era and pre-migration rows carry no
        # workshop context and must still be listed, just without names.
        .outerjoin(School, School.id == EmailSendLog.school_id)
        .outerjoin(Webinar, Webinar.id == EmailSendLog.webinar_id)
        .outerjoin(Workshop, Workshop.id == Webinar.workshop_id)
        .where(EmailSendLog.automation_id == automation_id)
        .order_by(EmailSendLog.sent_at.desc(), EmailSendLog.id.desc())
        .offset(offset)
        # Over-fetch by one to answer has_more without a second COUNT query.
        .limit(limit + 1)
    )
    if cycle_id is not None:
        stmt = stmt.where(Webinar.cycle_id == cycle_id)

    results = db.execute(stmt).all()
    has_more = len(results) > limit
    return AutomationSendPage(
        rows=[
            AutomationSendRow(
                id=log.id,
                recipient_email=log.recipient_email,
                subject=log.subject,
                status=log.status,
                sent_at=log.sent_at,
                school_name=school_name,
                workshop_name=workshop_name,
                webinar_start=webinar_start,
            )
            for log, school_name, workshop_name, webinar_start in results[:limit]
        ],
        has_more=has_more,
    )
