"""Name normalization used to match a college across yearly files.

The same rule is documented in docs/college-aid-csv-contract.md and imported by
the workbook converter, so the converter and the backend import never disagree.
"""

from __future__ import annotations

import re

_PUNCTUATION = re.compile(r"[^a-z0-9 ]")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Lowercase, '&' -> 'and', strip punctuation, collapse whitespace.

    Punctuation is dropped (not replaced by a space), so "King's College" and
    "Kings College" match; "St. John's" -> "st johns". Dashes inside words are
    dropped too, so use a space-separated dash ("A - B") when the source does.
    """
    text = name.lower().replace("&", " and ")
    text = _PUNCTUATION.sub("", text)
    return _SPACES.sub(" ", text).strip()
