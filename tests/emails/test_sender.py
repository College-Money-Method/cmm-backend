"""Tests for `emails.sender` — the per-send From identity.

An admin types any name/address they like; the domain allowlist is the actual
security boundary (presets are only suggestions), and `format_from_header` is
what finally reaches SES.
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.emails.sender import (
    InvalidSenderError,
    allowed_sender_domains,
    format_from_header,
    sender_presets,
    validate_sender,
)


def test_presets_include_configured_defaults():
    emails = {p["email"] for p in sender_presets()}
    assert "newsflash@collegemoneymethod.com" in emails
    assert "paul.martin@collegemoneymethod.com" in emails
    # The configured default is always offered, even if absent from the options.
    assert settings.ses_from_email in emails


def test_presets_deduplicate_by_address(monkeypatch):
    monkeypatch.setattr(
        settings,
        "ses_sender_options",
        "CMM <noreply@collegemoneymethod.com>,Other <noreply@collegemoneymethod.com>",
    )
    monkeypatch.setattr(settings, "ses_from_email", "noreply@collegemoneymethod.com")
    assert len(sender_presets()) == 1


def test_validate_accepts_any_address_on_an_allowed_domain():
    """Free-form is the point — an address that was never a preset is fine as
    long as the app is allowed to send as its domain."""
    assert validate_sender("Someone New", "someone.new@collegemoneymethod.com") == (
        "Someone New",
        "someone.new@collegemoneymethod.com",
    )


def test_validate_rejects_address_off_the_allowlist():
    with pytest.raises(InvalidSenderError):
        validate_sender("Spoof", "someone@evil.example")


def test_validate_rejects_malformed_address():
    with pytest.raises(InvalidSenderError):
        validate_sender("Broken", "not-an-address")


def test_validate_drops_a_name_with_no_address():
    """A display name alone has nothing to attach to — store neither half rather
    than a half-applied override."""
    assert validate_sender("Orphan Name", "") == (None, None)
    assert validate_sender(None, None) == (None, None)


def test_validate_strips_newlines_from_display_name():
    """A newline in the name would inject extra headers once formatted into the
    From line."""
    name, email = validate_sender("Evil\r\nBcc: victim@example.com", "ok@collegemoneymethod.com")
    assert "\n" not in name and "\r" not in name


def test_blank_allowlist_falls_back_to_the_default_senders_domain(monkeypatch):
    """A blanked env setting must not turn the allowlist off — that is the only
    guard against the app sending as an arbitrary third-party domain."""
    monkeypatch.setattr(settings, "ses_allowed_sender_domains", "")
    monkeypatch.setattr(settings, "ses_from_email", "noreply@collegemoneymethod.com")

    assert allowed_sender_domains() == ["collegemoneymethod.com"]
    with pytest.raises(InvalidSenderError):
        validate_sender("Spoof", "someone@evil.example")


def test_allowed_domains_lowercased_and_stripped(monkeypatch):
    monkeypatch.setattr(settings, "ses_allowed_sender_domains", " CollegeMoneyMethod.com , @Other.com ")
    assert allowed_sender_domains() == ["collegemoneymethod.com", "other.com"]


def test_format_header_quotes_a_name_containing_a_comma():
    header = format_from_header("Martin, Paul", "paul.martin@collegemoneymethod.com")
    assert header == '"Martin, Paul" <paul.martin@collegemoneymethod.com>'


def test_format_header_falls_back_to_the_configured_default():
    assert settings.ses_from_email in format_from_header(None, None)


def test_format_header_returns_bare_address_when_no_name(monkeypatch):
    monkeypatch.setattr(settings, "ses_from_name", "")
    assert format_from_header(None, "newsflash@collegemoneymethod.com") == (
        "newsflash@collegemoneymethod.com"
    )
