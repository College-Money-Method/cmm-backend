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
