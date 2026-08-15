"""Inline <style> CSS into element style attributes for email-client compatibility.

Most of the base shell already uses inline styles, but this pass covers any
future <style> additions and normalizes attribute styling so Outlook/Gmail
render consistently. premailer is pure-Python (no native build step), chosen
over css-inline for a simpler dependency footprint.
"""

from __future__ import annotations

from premailer import Premailer


def inline_css(html: str) -> str:
    """Inline all CSS rules from <style> blocks into each element's style attribute."""
    return Premailer(
        html,
        remove_classes=False,
        keep_style_tags=False,
        cssutils_logging_level="CRITICAL",
        disable_validation=True,
    ).transform()
