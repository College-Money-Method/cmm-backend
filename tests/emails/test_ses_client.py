"""Tests for ses_client.send_email's suppression / dry-run / live-send branching.

No real database or AWS account is used — `db` is a MagicMock standing in for
a SQLAlchemy Session (asserted against via `.add`/`.commit` calls), and the
boto3 client factory is patched so a "live send" test never makes a network call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import settings
from src.emails import ses_client
from src.emails.models import EmailSendLog


def _fake_db(suppressed: bool = False) -> MagicMock:
    db = MagicMock()
    db.scalar.return_value = "blocked@example.com" if suppressed else None
    return db


def test_dry_run_writes_log_and_makes_zero_boto3_calls(monkeypatch):
    monkeypatch.setattr(settings, "email_send_enabled", False)
    db = _fake_db()

    with patch.object(ses_client, "_create_ses_client") as mock_factory:
        log = ses_client.send_email(db, "user@example.com", "Subject", "<p>hi</p>", "hi", "broadcast")

    mock_factory.assert_not_called()
    assert log.status == "dry_run"
    assert log.rendered_html == "<p>hi</p>"
    added = db.add.call_args[0][0]
    assert isinstance(added, EmailSendLog)
    assert added.status == "dry_run"
    db.commit.assert_called_once()


def test_suppressed_recipient_is_skipped_even_when_send_enabled(monkeypatch):
    # Suppression must win regardless of email_send_enabled — un-bypassable.
    monkeypatch.setattr(settings, "email_send_enabled", True)
    db = _fake_db(suppressed=True)

    with patch.object(ses_client, "_create_ses_client") as mock_factory:
        log = ses_client.send_email(db, "blocked@example.com", "Subject", "<p>hi</p>", "hi", "broadcast")

    mock_factory.assert_not_called()
    assert log.status == "suppressed"


def test_live_send_calls_ses_and_logs_sent(monkeypatch):
    monkeypatch.setattr(settings, "email_send_enabled", True)
    monkeypatch.setattr(settings, "ses_configuration_set_name", "cmm")
    monkeypatch.setattr(settings, "ses_from_email", "noreply@collegemoneymethod.com")
    db = _fake_db()

    mock_client = MagicMock()
    mock_client.send_raw_email.return_value = {"MessageId": "abc-123"}

    with patch.object(ses_client, "_create_ses_client", return_value=mock_client):
        log = ses_client.send_email(db, "user@example.com", "Subject", "<p>hi</p>", "hi", "broadcast")

    mock_client.send_raw_email.assert_called_once()
    call_kwargs = mock_client.send_raw_email.call_args.kwargs
    assert call_kwargs["ConfigurationSetName"] == "cmm"
    assert call_kwargs["Destinations"] == ["user@example.com"]
    assert log.status == "sent"
    assert log.provider_message_id == "abc-123"


def test_sandbox_mode_blocks_recipient_outside_domain(monkeypatch):
    monkeypatch.setattr(settings, "email_send_enabled", True)
    monkeypatch.setattr(settings, "email_sandbox_mode", True)
    monkeypatch.setattr(settings, "email_sandbox_domain", "collegemoneymethod.com")
    db = _fake_db()

    with patch.object(ses_client, "_create_ses_client") as mock_factory:
        log = ses_client.send_email(db, "family@gmail.com", "Subject", "<p>hi</p>", "hi", "broadcast")

    mock_factory.assert_not_called()
    assert log.status == "sandboxed"
    assert log.rendered_html == "<p>hi</p>"


def test_sandbox_mode_allows_recipient_on_domain(monkeypatch):
    monkeypatch.setattr(settings, "email_send_enabled", True)
    monkeypatch.setattr(settings, "email_sandbox_mode", True)
    monkeypatch.setattr(settings, "email_sandbox_domain", "collegemoneymethod.com")
    db = _fake_db()

    mock_client = MagicMock()
    mock_client.send_raw_email.return_value = {"MessageId": "abc-123"}

    with patch.object(ses_client, "_create_ses_client", return_value=mock_client):
        log = ses_client.send_email(
            db, "Vu.Nguyen@CollegeMoneyMethod.com", "Subject", "<p>hi</p>", "hi", "broadcast"
        )

    mock_client.send_raw_email.assert_called_once()
    assert log.status == "sent"


def test_live_send_failure_logs_failed_and_reraises(monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.setattr(settings, "email_send_enabled", True)
    db = _fake_db()

    mock_client = MagicMock()
    mock_client.send_raw_email.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "SendRawEmail"
    )

    with patch.object(ses_client, "_create_ses_client", return_value=mock_client):
        try:
            ses_client.send_email(db, "user@example.com", "Subject", "<p>hi</p>", "hi", "broadcast")
            assert False, "expected ClientError to propagate"
        except ClientError:
            pass

    added = db.add.call_args[0][0]
    assert added.status == "failed"
