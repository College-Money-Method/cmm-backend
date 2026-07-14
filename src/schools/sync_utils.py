"""Shared field parsers for Airtable → DB sync modules."""
from __future__ import annotations


def parse_bool(value: object) -> bool:
    """Normalize Airtable checkbox fields (True/False/None/"true"/"false")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value) if value is not None else False


def parse_int(value: object) -> int | None:
    """Safely cast enrollment-style fields to int."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def detect_email_collisions(records: list[dict]) -> dict[str, list[str]]:
    """Map lowercased email → list of Airtable record IDs, for emails that
    appear on MORE than one contact record in a single pull (ISSUE-7).

    Used to process only the first occurrence deterministically and warn on the
    rest. Records with no email are ignored.
    """
    by_email: dict[str, list[str]] = {}
    for rec in records:
        email = (rec.get("fields", {}).get("Email") or "").strip().lower()
        if not email:
            continue
        by_email.setdefault(email, []).append(rec.get("id", ""))
    return {email: ids for email, ids in by_email.items() if len(ids) > 1}


def deactivation_is_safe(
    pulled_airtable_ids: set[str],
    known_airtable_ids: set[str],
    max_missing_fraction: float,
) -> bool:
    """Guard against wiping contacts on a partial/failed Airtable fetch.

    Returns False (skip deactivation) when the pull is empty while contacts
    exist, or when the fraction of known Airtable-linked contacts missing from
    the pull exceeds ``max_missing_fraction``.
    """
    if not known_airtable_ids:
        return True
    if not pulled_airtable_ids:
        return False
    missing = known_airtable_ids - pulled_airtable_ids
    return (len(missing) / len(known_airtable_ids)) <= max_missing_fraction


def should_revoke_access(
    role_user_id: str,
    role_name: str,
    active_user_ids: set[str],
    managed_user_ids: set[str],
) -> bool:
    """Decide whether a UserRole should be revoked during reconciliation.

    Revoke only sync-managed, non-super_admin roles whose driving contact is no
    longer active (school unlinked or contact deactivated). Admin-created roles
    (no backing contact) and super_admin are never touched.
    """
    if role_name == "super_admin":
        return False
    if role_user_id not in managed_user_ids:
        return False
    return role_user_id not in active_user_ids
