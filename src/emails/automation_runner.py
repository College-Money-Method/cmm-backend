"""General automation scheduler: runs every ENABLED `EmailAutomation` (any
`type`) against a unified `webinar.start_datetime ± offset` anchor.

Replaces the old pre-workshop-only `pre_workshop_reminder.py` now that
automations are full CRUD with a dynamic offset (value + unit + direction) and
multiple rows can share a type. Idempotency moved from the single-automation
`PortalMapping.pre_webinar_reminder_sent_on` column to a per-(automation,
portal_mapping) row in `automation_send_ledger` (see that model's docstring).

Due-window math
----------------
For automation A with `offset_value`/`offset_unit` combining into a
`timedelta` delta:
  - direction="before": fire_at = start - delta  (send BEFORE the workshop)
  - direction="after":  fire_at = start + delta  (send AFTER the workshop)

A mapping is "due" when `fire_at` falls in `[now - 2 days, now]` — i.e. it's
due right now, or was due within the last 2 days (a 2-day catch-up window so
an automation enabled mid-cycle still backfills recently-due mappings instead
of silently skipping them forever). "before" automations additionally require
`now < start` (never send a pre-workshop reminder after the workshop started).

Rather than computing `fire_at` as a SQL column expression (portable but
awkward across the Postgres prod DB and the SQLite test fixtures), the due
window is inverted algebraically into bounds on `Webinar.start_datetime`
itself — plain `>=`/`<=` comparisons against precomputed bind parameters,
which both dialects handle identically:
  - before: fire_at <= now <= fire_at + catch_up
            <=> now - catch_up + delta <= start <= now + delta
  - after:  fire_at <= now <= fire_at + catch_up
            <=> now - delta - catch_up <= start <= now - delta

Concurrency
-----------
Prod runs multiple worker processes (uvicorn WEB_CONCURRENCY>1, plus ECS
autoscaling), each booting its own in-process scheduler, so this job can fire
concurrently across processes. Exactly-once sending per (automation, mapping)
is enforced by a `SELECT ... FOR UPDATE SKIP LOCKED` claim on the mapping row
in `_process_due_mapping`, followed by a ledger-absence re-check: the first
process to claim a mapping holds the lock through its whole recipient batch
and inserts the ledger row before committing; a concurrent process's SKIP
LOCKED returns nothing (row locked) or, once the first commits, finds the
ledger row already present — either way it skips. (SQLite ignores FOR UPDATE,
fine for the single-connection test fixtures.) A crash mid-batch, before the
ledger insert commits, can still cause a partial duplicate on the next run —
accepted documented edge case, not worth distributed-transaction machinery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.base import get_session_factory
from src.emails.automation_ledger_models import AutomationSendLedger
from src.emails.automation_models import EmailAutomation
from src.emails.counselor_resolver import contact_is_school_counselor, resolve_counselor_name
from src.emails.email_template_models import EmailTemplate
from src.emails.link_resolver import resolve_plain_text
from src.emails.renderer import render_email
from src.emails.ses_client import _sandbox_enabled, send_email
from src.emails.unsubscribe import build_unsubscribe_url
from src.emails.workshop_merge_tags import build_workshop_merge_replacements
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop

logger = logging.getLogger(__name__)

_CATCH_UP_WINDOW = timedelta(days=2)

# Maps an automation's `type` to the EmailSendLog.source value its sends are
# logged under (see the `ck_email_send_log_source` check constraint).
_SOURCE_BY_TYPE = {
    "pre_workshop_reminder": "pre_workshop",
    "post_workshop_reminder": "post_workshop",
}


def run_automations_check(db: Session | None = None) -> int:
    """Entry point for the scheduler job (and for direct test invocation).

    Opens its own DB session when called by the scheduler (no request-scoped
    session exists on a background thread); tests may pass an explicit
    session bound to an in-memory fixture. Returns the number of emails
    sent/logged this run (dry-run counts too), mainly for test assertions.
    """
    if db is not None:
        return _run_automations_check(db)

    session_factory = get_session_factory()
    session = session_factory()
    try:
        return _run_automations_check(session)
    finally:
        session.close()


# Backward-compatible alias for the pre-Phase-6 name.
run_pre_workshop_check = run_automations_check


def _offset_timedelta(automation: EmailAutomation) -> timedelta:
    if automation.offset_unit == "hours":
        return timedelta(hours=automation.offset_value)
    return timedelta(days=automation.offset_value)


def _due_window(automation: EmailAutomation, now: datetime) -> tuple[datetime, datetime]:
    """Bounds on `Webinar.start_datetime` equivalent to `fire_at` falling in
    `[now - catch_up, now]` — see module docstring for the derivation."""
    delta = _offset_timedelta(automation)
    if automation.offset_direction == "before":
        return now + delta - _CATCH_UP_WINDOW, now + delta
    return now - delta - _CATCH_UP_WINDOW, now - delta


def _run_automations_check(db: Session) -> int:
    automations = list(db.scalars(select(EmailAutomation).where(EmailAutomation.enabled.is_(True))).all())
    # Resolve the sandbox flag ONCE per run — threaded down to every send so a
    # large recipient fan-out never re-queries the config per email.
    sandbox_enabled = _sandbox_enabled(db)
    return sum(_run_one_automation(db, automation, sandbox_enabled) for automation in automations)


def _run_one_automation(db: Session, automation: EmailAutomation, sandbox_enabled: bool) -> int:
    now = datetime.now(timezone.utc)
    lower, upper = _due_window(automation, now)

    already_sent = select(AutomationSendLedger.portal_mapping_id).where(
        AutomationSendLedger.automation_id == automation.id
    )

    conditions = [
        Webinar.start_datetime.is_not(None),
        Webinar.start_datetime >= lower,
        Webinar.start_datetime <= upper,
        PortalMapping.id.not_in(already_sent),
    ]
    if automation.offset_direction == "before":
        conditions.append(Webinar.start_datetime > now)

    due_mappings = list(
        db.scalars(
            select(PortalMapping).join(Webinar, PortalMapping.webinar_id == Webinar.id).where(*conditions)
        ).all()
    )

    return sum(
        _process_due_mapping(db, automation, mapping, sandbox_enabled) for mapping in due_mappings
    )


def _process_due_mapping(
    db: Session, automation: EmailAutomation, mapping: PortalMapping, sandbox_enabled: bool
) -> int:
    """Send `automation` to every opted-in contact of `mapping.school_id`,
    then record the (automation, mapping) pair in the ledger — only after the
    full recipient batch is attempted, so a partial batch never gets falsely
    marked done.

    Claims the mapping with a row lock first (see module docstring): if
    another worker process already holds it, or the ledger row was inserted
    since the outer query read it, skip without sending."""
    claimed = db.execute(
        select(PortalMapping.id).where(PortalMapping.id == mapping.id).with_for_update(skip_locked=True)
    ).first()
    if claimed is None:
        logger.info("automation %s: mapping %s locked by another worker, skipping", automation.id, mapping.id)
        return 0

    already_sent = db.scalar(
        select(AutomationSendLedger.id).where(
            AutomationSendLedger.automation_id == automation.id,
            AutomationSendLedger.portal_mapping_id == mapping.id,
        )
    )
    if already_sent is not None:
        logger.info("automation %s: mapping %s already in ledger, skipping", automation.id, mapping.id)
        return 0

    webinar = db.get(Webinar, mapping.webinar_id)
    school = db.get(School, mapping.school_id)
    if webinar is None or school is None:
        logger.warning("automation %s: mapping %s missing webinar/school, skipping", automation.id, mapping.id)
        return 0

    workshop = db.get(Workshop, webinar.workshop_id)
    if workshop is None:
        logger.warning("automation %s: webinar %s missing workshop, skipping", automation.id, webinar.id)
        return 0

    template = db.get(EmailTemplate, automation.template_id) if automation.template_id else None
    if template is None or template.category != "workshop":
        # No (valid) template configured yet — skip and leave no ledger row so
        # this mapping is retried automatically once a template is picked.
        logger.info(
            "automation %s: no workshop template configured, skipping (will retry)", automation.id
        )
        return 0

    # Mandatory opt-in filter — no override path exists for automations
    # (contrast with Broadcast's selectable opt_in_filter).
    recipients = list(
        db.scalars(
            select(Contact).where(
                Contact.school_id == school.id,
                Contact.auto_emails.is_(True),
                Contact.deleted_at.is_(None),
                Contact.email.is_not(None),
            )
        ).all()
    )

    base_counselor_first, base_counselor_last, base_counselor_name = resolve_counselor_name(
        db, school.id
    )
    family_label = school.nickname or (f"{school.name} families" if school.name else "families")
    resources = [{"id": str(a.id), "name": a.name, "link": a.link} for a in workshop.content_assets]
    subject = automation.subject_override or template.subject
    origin = settings.app_public_url or None
    source = _SOURCE_BY_TYPE.get(automation.type, "pre_workshop")

    sent = 0
    for contact in recipients:
        if contact.full_name and contact_is_school_counselor(contact, school.id):
            counselor_first = contact.first_name or ""
            counselor_last = contact.last_name or ""
            counselor_name = contact.full_name
        else:
            counselor_first = base_counselor_first
            counselor_last = base_counselor_last
            counselor_name = base_counselor_name
        replacements = build_workshop_merge_replacements(
            school_name=school.name,
            family_label=family_label,
            counselor_name=counselor_name,
            counselor_first_name=counselor_first,
            counselor_last_name=counselor_last,
            school_slug=school.slug,
            resource_center_url=school.school_resource_center_url,
            resource_center_password=school.cmm_website_password,
            workshop_name=workshop.name,
            webinar_id=webinar.id,
            start_datetime=webinar.start_datetime,
            suggested_grades=workshop.suggested_grades,
            cycle_name=webinar.cycle.name if webinar.cycle else None,
            registration_url=webinar.registration_url,
            resources=resources,
            origin=origin,
        )
        unsubscribe_url = build_unsubscribe_url(contact.id)
        html, text = render_email(
            template.body_json,
            replacements,
            subject,
            school_slug=school.slug,
            unsubscribe_url=unsubscribe_url,
            origin=origin,
        )
        resolved_subject = resolve_plain_text(subject, replacements)
        try:
            send_email(
                db,
                to=contact.email,
                subject=resolved_subject,
                html=html,
                text=text,
                source=source,
                unsubscribe_url=unsubscribe_url,
                automation_id=automation.id,
                sandbox_enabled=sandbox_enabled,
            )
            sent += 1
        except Exception:  # noqa: BLE001 - one recipient failure must not abort the batch
            logger.exception(
                "automation %s: send failed for contact %s (workshop %s)", automation.id, contact.id, workshop.id
            )

    db.add(AutomationSendLedger(automation_id=automation.id, portal_mapping_id=mapping.id))
    db.commit()
    return sent
