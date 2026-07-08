"""Default password derivation for hub (counselor/director) accounts."""

from __future__ import annotations


def default_hub_password(email: str, resource_center_password: str | None) -> str:
    """Default hub password: email local-part + the school's resource-center password.

    e.g. ``nnavu1@gmail.com`` with resource-center password ``hscmm`` yields
    ``nnavu1hscmm``. When the school has no resource-center password, the password
    is just the email handle (``nnavu1``) — never a Supabase invite.
    """
    handle = email.split("@", 1)[0]
    return f"{handle}{resource_center_password or ''}"
