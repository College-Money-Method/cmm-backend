"""SES Open/Click event ingestion (Phase 7 analytics fast-follow).

Runs from the same SNS webhook as bounce/complaint (``webhook_router.py``) but
kept in its own module so that file stays focused on signature verification
and top-level notification-type routing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.base import get_session_factory
from src.emails.models import EmailEvent, EmailSendLog

logger = logging.getLogger(__name__)


def _parse_ses_timestamp(raw: str | None) -> datetime:
    """Parse an SES event's ISO-8601 timestamp; falls back to now() if missing/invalid."""
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable SES event timestamp: %s", raw)
    return datetime.now(timezone.utc)


def _record_event(
    message_id: str | None, event_type: str, occurred_at_raw: str | None, url: str | None = None
) -> None:
    """Write one EmailEvent row for `message_id`, or skip gracefully when it
    can't be resolved to a known EmailSendLog — never raises, since a webhook
    500 would make SNS retry the same event indefinitely."""
    if not message_id:
        logger.info("SES %s event missing mail.messageId — skipping", event_type)
        return

    session_factory = get_session_factory()
    db = session_factory()
    try:
        send_log_id = db.scalar(
            select(EmailSendLog.id).where(EmailSendLog.provider_message_id == message_id)
        )
        if send_log_id is None:
            logger.info("SES %s event for unrecognized messageId — skipping", event_type)
            return
        db.add(
            EmailEvent(
                send_log_id=send_log_id,
                event_type=event_type,
                url=url,
                occurred_at=_parse_ses_timestamp(occurred_at_raw),
            )
        )
        db.commit()
    finally:
        db.close()


def handle_open_event(ses_message: dict) -> None:
    """Parse an SES `eventType=Open` payload and record an EmailEvent."""
    message_id = ses_message.get("mail", {}).get("messageId")
    occurred_at = ses_message.get("open", {}).get("timestamp")
    _record_event(message_id, "open", occurred_at)


def handle_click_event(ses_message: dict) -> None:
    """Parse an SES `eventType=Click` payload and record an EmailEvent with the clicked URL."""
    message_id = ses_message.get("mail", {}).get("messageId")
    click = ses_message.get("click", {})
    _record_event(message_id, "click", click.get("timestamp"), url=click.get("link"))
