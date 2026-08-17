"""SES bounce/complaint webhook via SNS HTTPS delivery.

SNS cannot send a bearer token, so authenticity is established by verifying
the message signature (SNS Signature Version 1/2) against a certificate SNS
itself publishes — the JSON body is never trusted on its own. An unverified
payload would let anyone suppress arbitrary email addresses by POSTing a
forged bounce/complaint notification.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from functools import lru_cache

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import Certificate, load_pem_x509_certificate
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import settings
from src.db.base import get_session_factory
from src.emails.models import EmailSuppression
from src.emails.webhook_events import handle_click_event, handle_open_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emails", tags=["email-webhooks"])

# SNS only ever serves signing certs from its own regional endpoints — this
# allow-list stops a forged payload from pointing SigningCertURL at an
# attacker-controlled host.
_SIGNING_CERT_URL_RE = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/.*\.pem$")

_HTTP_TIMEOUT_SECONDS = 5.0


@lru_cache(maxsize=8)
def _fetch_signing_cert(url: str) -> Certificate:
    """Fetch + parse an SNS signing certificate (cached by URL)."""
    response = httpx.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return load_pem_x509_certificate(response.content)


def _signable_string(message: dict) -> bytes:
    """Build the newline-joined key/value string SNS signs, per message type."""
    if message.get("Type") == "Notification":
        keys = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
    else:
        keys = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")

    parts: list[str] = []
    for key in keys:
        if key in message:
            parts.append(key)
            parts.append(str(message[key]))
    return ("\n".join(parts) + "\n").encode("utf-8")


def _verify_sns_signature(message: dict) -> bool:
    """Verify an SNS message's signature against its published signing cert."""
    signing_cert_url = message.get("SigningCertURL", "")
    if not _SIGNING_CERT_URL_RE.match(signing_cert_url):
        logger.warning("Rejected SNS message: SigningCertURL not allow-listed: %s", signing_cert_url)
        return False
    if "Signature" not in message:
        logger.warning("Rejected SNS message: missing Signature field")
        return False

    try:
        cert = _fetch_signing_cert(signing_cert_url)
        signature = base64.b64decode(message["Signature"])
        signature_version = message.get("SignatureVersion", "1")
        hash_algorithm = hashes.SHA256() if signature_version == "2" else hashes.SHA1()
        cert.public_key().verify(
            signature,
            _signable_string(message),
            padding.PKCS1v15(),
            hash_algorithm,
        )
        return True
    except (InvalidSignature, KeyError, ValueError, httpx.HTTPError) as exc:
        logger.warning("Rejected SNS message: signature verification failed: %s", exc)
        return False


def _upsert_suppression(email: str, reason: str) -> None:
    """Insert a suppression row for `email` (no-op if one already exists)."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        stmt = (
            pg_insert(EmailSuppression)
            .values(email=email, reason=reason)
            .on_conflict_do_nothing(index_elements=["email"])
        )
        db.execute(stmt)
        db.commit()
    finally:
        db.close()


def _handle_notification(message: dict) -> None:
    """Parse an SES notification: bounce/complaint suppress the recipient(s);
    open/click (Phase 7) write an EmailEvent row for analytics.

    SES event-publishing payloads use ``eventType`` (present on every event
    type, including Open/Click); bounce/complaint additionally carry the older
    ``notificationType`` field for backward compatibility, so preferring
    ``eventType`` covers both without changing existing bounce/complaint
    behavior.
    """
    try:
        ses_message = json.loads(message.get("Message", "{}"))
    except json.JSONDecodeError:
        logger.error("SNS Notification.Message was not valid JSON")
        return

    notification_type = ses_message.get("eventType") or ses_message.get("notificationType")

    if notification_type == "Open":
        handle_open_event(ses_message)
        return
    if notification_type == "Click":
        handle_click_event(ses_message)
        return

    if notification_type == "Bounce":
        recipients = ses_message.get("bounce", {}).get("bouncedRecipients", [])
        reason = "bounce"
    elif notification_type == "Complaint":
        recipients = ses_message.get("complaint", {}).get("complainedRecipients", [])
        reason = "complaint"
    else:
        logger.info("Ignoring SES notification type=%s", notification_type)
        return

    for recipient in recipients:
        email = recipient.get("emailAddress")
        if email:
            _upsert_suppression(email, reason)
            logger.info("Suppressed recipient (reason=%s)", reason)


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def ses_sns_webhook(request: Request):
    """SNS delivery endpoint for SES bounce/complaint events on the shared
    Configuration Set's event destination.

    Handles subscription bootstrapping (confirms via SubscribeURL) and
    Notification (bounce/complaint) events. Always verifies the message
    signature first — no other authentication is possible for SNS HTTPS delivery.
    """
    raw_body = await request.body()
    try:
        message = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    if not _verify_sns_signature(message):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid SNS signature")

    # A valid AWS signature only proves *some* AWS-owned SNS topic sent this — an
    # attacker can create their own topic, subscribe it to this endpoint, and
    # publish genuinely-signed messages. Pinning TopicArn to our own event topic
    # stops a forged bounce/complaint from suppressing arbitrary contacts and
    # stops an attacker-owned topic from auto-confirming its subscription.
    expected_topic = settings.ses_sns_topic_arn
    if expected_topic and message.get("TopicArn") != expected_topic:
        logger.warning("Rejected SNS message: TopicArn mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unexpected SNS topic")

    message_type = request.headers.get("x-amz-sns-message-type", message.get("Type", ""))

    if message_type == "SubscriptionConfirmation":
        subscribe_url = message.get("SubscribeURL", "")
        try:
            httpx.get(subscribe_url, timeout=_HTTP_TIMEOUT_SECONDS)
            logger.info("Confirmed SNS subscription")
        except httpx.HTTPError as exc:
            logger.error("Failed to confirm SNS subscription: %s", exc)
        return {"status": "ok"}

    if message_type == "Notification":
        _handle_notification(message)
        return {"status": "ok"}

    logger.info("Ignoring SNS message type=%s", message_type)
    return {"status": "ok"}
