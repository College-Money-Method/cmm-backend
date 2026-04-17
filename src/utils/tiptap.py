"""Utilities for extracting plain text from TipTap JSON documents."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _extract_node(node: dict) -> str:
    """Recursively extract plain text from a single TipTap node."""
    if node.get("type") == "text":
        return node.get("text", "")
    children = node.get("content") or []
    return " ".join(part for child in children if (part := _extract_node(child).strip()))


def extract_text(value: str | dict | None) -> str:
    """
    Safely extract plain text from a TipTap JSON field.

    Accepts a dict (already parsed by SQLAlchemy) or a raw JSON string.
    Returns an empty string on any error — never raises, so saves are never blocked.
    """
    if not value:
        return ""
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return ""
    try:
        node = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        # Field contains plain text (not TipTap JSON) — return as-is
        return value if isinstance(value, str) else ""
    try:
        return _extract_node(node).strip()
    except Exception:
        logger.warning("Failed to extract text from TipTap node", exc_info=True)
        return ""
