"""Re-cut Zoom's transcript cues into sentences, the unit a trailer is edited in.

Zoom merges speech into cues of ~10 seconds (up to ~24) that start and stop
mid-sentence — "Effectively, financial aid" / "typically comes in two forms…".
Cutting on those boundaries clips thoughts in half, and asking a model to build
a 10-second segment out of 20-second cues cannot work. Sentences are what an
editor cuts on, and most run 3-8 seconds.

Zoom gives no word timings, so each word's time is interpolated from where its
characters sit within its cue: speech rate is steady enough inside one cue that
the estimate lands within about a second. That residual error is why the cuts
are then snapped to the nearest pause in the audio (``snap_to_pause``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.video_pipeline.ffmpeg_ops import _run
from src.video_pipeline.transcript import Cue

# Zoom prefixes each cue with its speaker: "Paul Martin, College Money Method: …".
_SPEAKER_RE = re.compile(r"^([^:.?!]{1,80}):\s+")
_SENTENCE_END = (".", "?", "!")
_SILENCE_RE = re.compile(r"silence_(start|end): (-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class _Word:
    start: float
    end: float
    text: str
    speaker: str


def split_sentences(cues: list[Cue]) -> list[Cue]:
    """Sentences with estimated times. The first sentence of each speaker turn
    keeps the "Name: " prefix, so the model can still tell presenter from attendee."""
    sentences: list[Cue] = []
    current: list[_Word] = []
    previous_speaker = None

    def flush() -> None:
        nonlocal previous_speaker
        if not current:
            return
        speaker = current[0].speaker
        text = " ".join(word.text for word in current)
        if speaker and speaker != previous_speaker:
            text = f"{speaker}: {text}"
        previous_speaker = speaker
        sentences.append(Cue(start=round(current[0].start, 3),
                             end=round(current[-1].end, 3), text=text))
        current.clear()

    for word in _words(cues):
        if current and word.speaker != current[-1].speaker:
            flush()
        current.append(word)
        if word.text.endswith(_SENTENCE_END):
            flush()
    flush()
    return sentences


def speakers(sentences: list[Cue]) -> list[str]:
    """Who says each sentence, carried forward from the last "Name: " prefix.

    "" until the first labelled turn. Zoom labels a speaker "Name, Company", so
    this is the whole label, not just the name.
    """
    current, names = "", []
    for sentence in sentences:
        match = _SPEAKER_RE.match(sentence.text)
        if match:
            current = match.group(1).strip()
        names.append(current)
    return names


def _words(cues: list[Cue]) -> list[_Word]:
    words: list[_Word] = []
    speaker = ""
    for cue in cues:
        text = cue.text
        match = _SPEAKER_RE.match(text)
        if match:
            speaker, text = match.group(1).strip(), text[match.end():]
        tokens = text.split()
        total = sum(len(token) + 1 for token in tokens) or 1
        span = cue.end - cue.start
        offset = 0
        for token in tokens:
            start = cue.start + span * offset / total
            offset += len(token) + 1
            words.append(_Word(start, cue.start + span * offset / total, token, speaker))
    return words


def snap_to_pause(source: Path, at: float, *, before: float, after: float) -> float:
    """The middle of the pause nearest `at` within [at-before, at+after], or `at`.

    Asymmetric windows keep a snap from walking into the sentence: a start
    searches mostly earlier, an end mostly later.
    """
    lo = max(0.0, at - before)
    proc = _run([
        "ffmpeg", "-v", "info", "-nostats", "-ss", f"{lo:.3f}", "-t", f"{before + after:.3f}",
        "-i", str(source), "-vn", "-af", "silencedetect=noise=-32dB:d=0.12", "-f", "null", "-",
    ])
    pauses: list[float] = []
    opened: float | None = None
    for kind, value in _SILENCE_RE.findall(proc.stderr):
        if kind == "start":
            opened = float(value)
        elif opened is not None:
            pauses.append(lo + (opened + float(value)) / 2)
            opened = None
    if not pauses:
        return at
    return min(pauses, key=lambda t: abs(t - at))
