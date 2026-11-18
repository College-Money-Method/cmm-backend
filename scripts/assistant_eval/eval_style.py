"""CMM house-style checks a script can make without a model.

Mirrors the "Voice and style" team guidance: no em or en dashes and none of
the hype phrases CMM avoids. Bold and headings are allowed in moderation, so
the judge's voice score covers them rather than a pattern. Reported next to the pass rate;
style never fails a case on its own, because substance is graded separately.
"""

from __future__ import annotations

import re
from collections import Counter

_RULES: list[tuple[str, re.Pattern]] = [
    # An em or en dash, or a spaced hyphen standing in for one (list bullets start a line, so they never match).
    ("dash", re.compile(r"[—–]|(?<=\S) - (?=\S)")),
    ("hype phrase", re.compile(
        r"\b(maximi[sz]\w*|optimi[sz]\w*|leverag\w*|unlock\w*|hidden money|hacks?|secret strateg\w*)\b",
        re.IGNORECASE,
    )),
]


def style_issues(answer: str) -> list[str]:
    """Names of the house-style rules the answer breaks, each once."""
    return [name for name, pattern in _RULES if pattern.search(answer)]


def style_summary(results: list[dict]) -> str:
    """One report line: clean answers and how often each rule was broken."""
    graded = [r for r in results if "checks" in r]
    counts = Counter(issue for r in graded for issue in r["checks"].get("style", []))
    clean = sum(not r["checks"].get("style") for r in graded)
    voices = [r["verdict"]["voice"] for r in graded if isinstance(r.get("verdict", {}).get("voice"), int)]
    voice = f" · avg voice {sum(voices) / len(voices):.1f}" if voices else ""
    broken = ", ".join(f"{name} {n}" for name, n in counts.most_common()) or "none"
    return f"Style: {clean}/{len(graded)} answers clean ({broken}){voice}"
