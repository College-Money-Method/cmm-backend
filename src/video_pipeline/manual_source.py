"""Read what an operator pasted into the "new run" box.

One field accepts three things, because an operator has whichever one is to
hand: a Zoom meeting/webinar ID, a Zoom recording UUID, or a direct download
URL. Sorting them out is pure string work and lives here so the endpoint can
reject a bad paste with a specific message instead of failing later inside an
ECS task, where the only symptom would be a stack trace on a job row.

Two Zoom forms deliberately do **not** parse:

* ``zoom.us/rec/share/...`` and ``zoom.us/rec/play/...`` carry an opaque
  per-share token. There is no API that turns one back into a recording, so
  accepting it would mean promising a run that cannot start.
* A join link's password query (``?pwd=``) is ignored — the API resolves a
  recording from the meeting ID under the account's own credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# Kinds of source a run can have.
ZOOM = "zoom"
URL = "url"

# Zoom meeting and webinar IDs are 9-11 digits. Operators paste them with the
# spaces Zoom's own UI shows ("881 2345 6789") and sometimes with dashes.
_ZOOM_ID = re.compile(r"^\d{9,11}$")
_SEPARATORS = re.compile(r"[\s\-]")

# Recording UUIDs are base64 with the padding left on, and a leading "/" or an
# embedded "//" is exactly why zoom.encode_recording_uuid double-encodes.
_RECORDING_UUID = re.compile(r"^[A-Za-z0-9+/=]{16,}$")

# Numeric-id join and management links. The id is the last path segment.
_ZOOM_LINK_PATH = re.compile(r"/(?:j|w|s|webinar|meeting)/(\d{9,11})")
_UNRESOLVABLE_ZOOM_PATH = re.compile(r"/rec/(?:share|play)/")


class SourceError(ValueError):
    """The pasted source is not something a run can be started from."""


@dataclass(frozen=True)
class ManualSource:
    """What to fetch, once the paste has been understood.

    Exactly one of the two references is set: ``zoom_reference`` is a meeting ID
    or recording UUID for the Zoom API to resolve, ``url`` is bytes to download.
    """

    kind: str
    zoom_reference: str = ""
    url: str = ""

    @property
    def is_zoom(self) -> bool:
        return self.kind == ZOOM


def _zoom_from_link(parsed) -> ManualSource:
    if _UNRESOLVABLE_ZOOM_PATH.search(parsed.path):
        raise SourceError(
            "A Zoom share or play link cannot be resolved through the API — it is a "
            "per-share token, not a recording id. Paste the meeting/webinar ID or the "
            "recording UUID instead (Zoom → Recordings → the session)."
        )
    match = _ZOOM_LINK_PATH.search(parsed.path)
    if not match:
        raise SourceError(
            f"No Zoom meeting or webinar ID found in '{parsed.geturl()}'. Paste the "
            "numeric ID, or the recording UUID."
        )
    return ManualSource(kind=ZOOM, zoom_reference=match.group(1))


def parse_source(raw: str) -> ManualSource:
    """Work out what ``raw`` names, or raise :class:`SourceError` saying why not.

    Ordering matters: a URL is decided by its scheme before anything else, so a
    numeric id inside a link is never mistaken for a download URL and a Zoom
    host is never mistaken for a plain one.
    """
    value = (raw or "").strip()
    if not value:
        raise SourceError("Paste a Zoom meeting ID, a Zoom recording UUID, or a download URL")

    if "://" in value or value.lower().startswith(("http:", "https:")):
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise SourceError(
                f"Only https URLs are accepted (got '{parsed.scheme or 'no scheme'}')"
            )
        if not parsed.hostname:
            raise SourceError(f"'{value}' has no host")
        host = parsed.hostname.lower()
        if host == "zoom.us" or host.endswith(".zoom.us"):
            return _zoom_from_link(parsed)
        return ManualSource(kind=URL, url=value)

    compact = _SEPARATORS.sub("", value)
    if _ZOOM_ID.match(compact):
        return ManualSource(kind=ZOOM, zoom_reference=compact)
    if _RECORDING_UUID.match(value):
        return ManualSource(kind=ZOOM, zoom_reference=value)

    raise SourceError(
        f"'{value}' is not a Zoom meeting ID (9-11 digits), a recording UUID, or an https URL"
    )
