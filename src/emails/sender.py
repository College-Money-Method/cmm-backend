"""Per-send "From" identity: presets, validation, and header formatting.

An admin may type any display name and any address for a broadcast or
automation — but only on a domain the app is allowed to send as
(``settings.ses_allowed_sender_domains``). That guard exists because SES will
reject an unverified identity at send time anyway: rejecting at save time turns
a silent, per-recipient send failure into one actionable 400, and stops the app
being used to send as an arbitrary third-party domain.

``settings.ses_sender_options`` supplies the presets the compose UI offers
(``"Name <email>"``, comma-separated). They are suggestions only — the
allowlist, not the preset list, is the security boundary.

A separate, much narrower list (``settings.ses_no_unsubscribe_senders``) marks
the senders whose mail goes out with no unsubscribe mechanism at all — see
``sender_omits_unsubscribe``.
"""

from __future__ import annotations

import re
from email.headerregistry import Address
from email.utils import parseaddr

from src.config import settings

# Deliberately permissive: real deliverability is decided by the domain
# allowlist below plus SES identity verification, not by regex pedantry.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidSenderError(ValueError):
    """Raised when a caller-supplied sender address is malformed or off-domain."""


def allowed_sender_domains() -> list[str]:
    """Lower-cased domains the app may send as.

    Blanking the setting must NOT disable the allowlist — this is the only thing
    stopping the app being used to send as an arbitrary third-party domain, so a
    misconfigured env falls back to the configured default sender's own domain
    rather than failing open.
    """
    raw = settings.ses_allowed_sender_domains or ""
    domains = [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]
    if domains:
        return domains
    default_domain = (settings.ses_from_email or "").rsplit("@", 1)[-1].strip().lower()
    return [default_domain] if default_domain else []


def sender_presets() -> list[dict[str, str]]:
    """The From options the compose UI offers, newest-first as configured.

    The configured default (``settings.ses_from_email``) is always present, so
    the picker never comes up empty on an environment with no presets set.
    """
    presets: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(name: str, email: str) -> None:
        key = email.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        presets.append({"name": name.strip(), "email": email.strip()})

    for entry in (settings.ses_sender_options or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, email = parseaddr(entry)
        _add(name, email or entry)

    _add(settings.ses_from_name or "College Money Method", settings.ses_from_email)
    return presets


def no_unsubscribe_senders() -> set[str]:
    """Lower-cased From addresses configured to send without any unsubscribe.

    Unlike ``allowed_sender_domains``, an empty setting here means the empty
    set: this list REMOVES a safeguard, so a misconfigured env must fall back to
    "everyone gets an unsubscribe link", never to "nobody does".
    """
    raw = settings.ses_no_unsubscribe_senders or ""
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


def sender_omits_unsubscribe(email: str | None) -> bool:
    """True when mail from this sender carries NO unsubscribe mechanism.

    ``email`` is the send's own sender override, or ``None``/blank to mean the
    configured default — resolved the same way ``format_from_header`` resolves
    it, so the default sender is checked under its real address rather than
    silently skipping the list.

    Callers act on this by passing ``unsubscribe_url=None`` down the render +
    send path, which drops BOTH the visible footer link (``base.html`` renders
    it only when a URL is supplied) and the one-click ``List-Unsubscribe``
    header (``ses_client._build_message``) in a single place.
    """
    address = (email or "").strip() or (settings.ses_from_email or "")
    return address.lower() in no_unsubscribe_senders()


def validate_sender(name: str | None, email: str | None) -> tuple[str | None, str | None]:
    """Normalize and validate an admin-chosen sender.

    Returns the ``(name, email)`` to store — ``(None, None)`` when no address was
    given, meaning "use the configured default at send time". Raises
    ``InvalidSenderError`` on a malformed address or one outside the allowed
    sending domains.
    """
    email = (email or "").strip()
    name = (name or "").strip()
    if not email:
        # A display name with no address has nothing to attach to; drop both
        # rather than half-apply an override.
        return None, None

    if not _EMAIL_RE.match(email):
        raise InvalidSenderError(f"'{email}' is not a valid email address")

    domains = allowed_sender_domains()
    if domains and email.rsplit("@", 1)[1].lower() not in domains:
        raise InvalidSenderError(
            f"Sender must be on one of: {', '.join(domains)} — '{email}' is not"
        )

    # A name containing a newline could inject extra headers once formatted into
    # the From line; strip CR/LF rather than reject, since it is almost always a
    # paste artifact.
    return (name.replace("\r", " ").replace("\n", " ").strip() or None), email


def format_from_header(name: str | None, email: str | None) -> str:
    """Build the RFC 5322 ``From`` value, falling back to the configured default.

    ``Address`` handles the quoting/encoding of display names containing commas,
    quotes, or non-ASCII characters.
    """
    address = (email or "").strip() or settings.ses_from_email
    display = (name or "").strip()
    if not display and address == settings.ses_from_email:
        display = (settings.ses_from_name or "").strip()
    if not display:
        return address
    local, _, domain = address.partition("@")
    return str(Address(display_name=display, username=local, domain=domain))
