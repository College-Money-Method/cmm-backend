"""AWS SES client and email-send orchestration (suppression + sandbox + real send).

Mirrors ``src.storage.s3_client``'s ``@lru_cache`` boto3-client-factory pattern.
``send_email`` is the single entry point every sender (broadcast, pre-workshop,
followup) must call — it enforces two un-bypassable checks, in order, before
ever talking to SES:

  1. Suppression — an address that bounced/complained/unsubscribed is skipped.
  2. Sandbox — when the global ``AppConfig.email_sandbox_mode`` flag is on, a
     recipient outside the team domain (``SANDBOX_DOMAIN``) is logged
     (status="sandboxed") but never sent, on ANY environment.
"""

from __future__ import annotations

import logging
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from typing import Annotated

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.app_config.models import AppConfig
from src.config import settings
from src.emails.models import EmailSendLog, EmailSuppression

logger = logging.getLogger(__name__)

# The team domain that email sandbox mode allows through. Recipients here are
# real teammates, safe to send to while sandboxing; everyone else is withheld.
SANDBOX_DOMAIN = "collegemoneymethod.com"


@lru_cache
def _create_ses_client() -> BaseClient:
    """Create boto3 SES client (cached). Uses settings for credentials and region."""
    return boto3.client(
        "ses",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


def ses_client() -> BaseClient:
    """Return the shared SES client (for use outside FastAPI)."""
    return _create_ses_client()


def get_ses_client() -> BaseClient:
    """FastAPI dependency that returns the SES client."""
    return _create_ses_client()


SesClientDep = Annotated[BaseClient, Depends(get_ses_client)]


def _is_suppressed(db: Session, to: str) -> bool:
    return db.scalar(select(EmailSuppression.email).where(EmailSuppression.email == to)) is not None


def _in_sandbox_domain(to: str) -> bool:
    """True when `to` is on the team sandbox domain (case-insensitive)."""
    return to.strip().lower().endswith("@" + SANDBOX_DOMAIN)


def _sandbox_enabled(db: Session) -> bool:
    """Read the runtime email-sandbox flag from the global app config.

    Missing config row (fresh DB) resolves to False — i.e. production sending —
    matching the column default. Isolated as its own function so tests can patch
    it without seeding an AppConfig row."""
    return bool(db.scalar(select(AppConfig.email_sandbox_mode)))


def _build_raw_message(
    recipients: list[str],
    subject: str,
    html: str,
    text: str,
    *,
    unsubscribe_url: str | None = None,
    from_address: str | None = None,
) -> bytes:
    """Build a multipart/alternative MIME message.

    Raw (``send_raw_email``, not the simple ``send_email`` API) so the
    ``List-Unsubscribe`` header can be attached here, one-click, per RFC 8058 —
    every sender (broadcast, and future pre-workshop/followup) gets it for free
    by passing ``unsubscribe_url`` through to ``send_email``.

    ``recipients`` may hold several addresses (a grouped send to every counselor
    at one school): they all go on a single ``To`` header so the recipients can
    see each other, which is the point of grouping.
    """
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = from_address or settings.ses_from_email
    message["To"] = ", ".join(recipients)
    if unsubscribe_url:
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))
    return message.as_bytes()


def _log_send(
    db: Session,
    *,
    to: str,
    subject: str,
    status: str,
    source: str,
    provider_message_id: str | None = None,
    rendered_html: str | None = None,
    broadcast_id: uuid.UUID | None = None,
    automation_id: uuid.UUID | None = None,
) -> EmailSendLog:
    log = EmailSendLog(
        recipient_email=to,
        subject=subject,
        status=status,
        source=source,
        provider_message_id=provider_message_id,
        rendered_html=rendered_html,
        broadcast_id=broadcast_id,
        automation_id=automation_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def send_email(
    db: Session,
    to: str | list[str],
    subject: str,
    html: str,
    text: str,
    source: str,
    tags: dict[str, str] | None = None,
    *,
    broadcast_id: uuid.UUID | None = None,
    automation_id: uuid.UUID | None = None,
    unsubscribe_url: str | None = None,
    sandbox_enabled: bool | None = None,
    from_address: str | None = None,
) -> EmailSendLog:
    """Send one email, or log it, depending on suppression / sandbox state.

    Checks run in this order, both un-bypassable by any caller:
      1. Suppression: a recipient with an active ``EmailSuppression`` row is
         dropped and logged with status "suppressed", no network call.
      2. Sandbox: when ``AppConfig.email_sandbox_mode`` is on, recipients
         outside ``SANDBOX_DOMAIN`` are dropped and logged with status
         "sandboxed" (rendered HTML attached), no boto3 call.
      3. Whoever survives both is sent to via SES ``send_raw_email`` through the
         shared Configuration Set and logged "sent" (or "failed" on a boto3 error).

    ``to`` may be a list — a grouped broadcast addressing every counselor at one
    school in a single email. The drops above are evaluated per address, so one
    unsubscribed counselor no longer blocks the mail to their colleagues, and
    every address gets its own log row whatever its outcome, keeping the
    per-broadcast status counts honest. The returned row is the last one of the
    bucket that got furthest (sent > sandboxed > suppressed).

    ``broadcast_id`` (source="broadcast" callers) and ``automation_id``
    (source="pre_workshop"/"post_workshop" callers) link the logged row back
    to the Broadcast/EmailAutomation it was sent as part of — mutually
    exclusive in practice, both accepted here for a single shared log path.
    ``unsubscribe_url``, when given, is attached as a one-click
    ``List-Unsubscribe`` header on the raw message — every sender that resolves
    an unsubscribe link gets CAN-SPAM compliance for free.

    ``sandbox_enabled`` lets a batch sender resolve the global flag ONCE per
    batch and pass the decision in, avoiding a per-recipient config query on a
    large fan-out. Left as None (single/ad-hoc sends), the flag is read from the
    DB here.

    ``from_address`` overrides the configured default sender for this one
    message (already validated + formatted by ``emails.sender``).
    """
    recipients = [to] if isinstance(to, str) else [address for address in to if address]
    if not recipients:
        raise ValueError("send_email requires at least one recipient")

    def _log(status: str, addresses: list[str], *, rendered_html: str | None = None,
             provider_message_id: str | None = None) -> EmailSendLog:
        """Log ONE row per address, even when they shared a single grouped email.

        Every consumer of ``email_send_log`` (per-broadcast status counts, the
        recipient table, open/click rates) treats a row as one recipient, so a
        comma-joined row would undercount a grouped send's reach. Rows from the
        same grouped send share ``provider_message_id`` — SES issues one id for
        the message, and an open/click event on it can only be attributed to the
        group, not to an individual.
        """
        rows = [
            _log_send(
                db,
                to=address,
                subject=subject,
                status=status,
                source=source,
                provider_message_id=provider_message_id,
                rendered_html=rendered_html,
                broadcast_id=broadcast_id,
                automation_id=automation_id,
            )
            for address in addresses
        ]
        return rows[-1]

    suppressed = [address for address in recipients if _is_suppressed(db, address)]
    remaining = [address for address in recipients if address not in suppressed]
    last_log: EmailSendLog | None = None
    if suppressed:
        logger.info("Skipping send to %d suppressed recipient(s) (source=%s)", len(suppressed), source)
        last_log = _log("suppressed", suppressed)
    if not remaining:
        assert last_log is not None  # non-empty recipients with none remaining => all suppressed
        return last_log

    in_sandbox = _sandbox_enabled(db) if sandbox_enabled is None else sandbox_enabled
    if in_sandbox:
        withheld = [address for address in remaining if not _in_sandbox_domain(address)]
        remaining = [address for address in remaining if _in_sandbox_domain(address)]
        if withheld:
            logger.info(
                "email_sandbox_mode — %d recipient(s) outside %s, not sent (source=%s)",
                len(withheld),
                SANDBOX_DOMAIN,
                source,
            )
            last_log = _log("sandboxed", withheld, rendered_html=html)
        if not remaining:
            assert last_log is not None
            return last_log

    client = _create_ses_client()
    raw_message = _build_raw_message(
        remaining, subject, html, text, unsubscribe_url=unsubscribe_url, from_address=from_address
    )

    kwargs: dict = {
        "Source": from_address or settings.ses_from_email,
        "Destinations": remaining,
        "RawMessage": {"Data": raw_message},
    }
    if settings.ses_configuration_set_name:
        kwargs["ConfigurationSetName"] = settings.ses_configuration_set_name
    if tags:
        kwargs["Tags"] = [{"Name": key, "Value": value} for key, value in tags.items()]

    try:
        response = client.send_raw_email(**kwargs)
    except (BotoCoreError, ClientError) as exc:
        logger.error("SES send failed (source=%s): %s", source, exc)
        _log("failed", remaining)
        raise

    return _log("sent", remaining, provider_message_id=response.get("MessageId"))
