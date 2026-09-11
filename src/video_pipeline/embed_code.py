"""Build the iframe snippet stored in ``webinars.video_embed_code``.

The site renders this column as raw HTML, and the front-end parses it back out
again to read the video id and player options, so the shape is a contract with
code on both sides rather than free-form markup. It is modelled on the snippets
already in the table — same attributes, same option order, ``&amp;`` between
query parameters — so a generated row is indistinguishable from one an admin
pasted from Vimeo.

The player URL always comes from what the API returned at upload. Hand-building
``/video/{id}?h={hash}`` looks equivalent and breaks the day Vimeo changes how
an embed-only video is addressed.
"""

from __future__ import annotations

from html import escape
from urllib.parse import parse_qsl, urlsplit

# Vimeo's own "copy embed code" defaults, in the order Vimeo emits them. Chrome
# is off (no title, byline, portrait or badge) because the page around the
# player already says what the webinar is; autopause off so a second player on
# the page does not stop this one.
PLAYER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("title", "0"),
    ("byline", "0"),
    ("portrait", "0"),
    ("badge", "0"),
    ("autopause", "0"),
    ("player_id", "0"),
    ("app_id", "58479"),
)

# The recordings are 1080p and the surrounding CSS scales the iframe down, so
# these are an aspect ratio as much as a size.
WIDTH = "1920"
HEIGHT = "1080"

ALLOW = "autoplay; fullscreen; picture-in-picture; clipboard-write; encrypted-media; web-share"
REFERRER_POLICY = "strict-origin-when-cross-origin"


def player_src(player_embed_url: str) -> str:
    """Append the player options to Vimeo's embed URL, keeping what it carries.

    Anything already on the URL — the privacy hash above all — is preserved and
    kept first, and an option Vimeo already set is not overridden.
    """
    if not player_embed_url:
        raise ValueError("Vimeo returned no player_embed_url for this video")

    parts = urlsplit(player_embed_url)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    present = {key for key, _ in existing}
    merged = existing + [(k, v) for k, v in PLAYER_OPTIONS if k not in present]

    query = "&".join(f"{key}={value}" for key, value in merged)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{base}?{query}" if query else base


def build_embed_code(player_embed_url: str, title: str) -> str:
    """The iframe to store on the webinar row.

    ``title`` becomes the iframe's accessible name, so it is HTML-escaped: a
    webinar called ``Grants & Scholarships`` would otherwise produce markup that
    a strict parser reads as a broken attribute.
    """
    src = escape(player_src(player_embed_url), quote=True)
    return (
        f'<iframe src="{src}" width="{WIDTH}" height="{HEIGHT}" frameborder="0" '
        f'allow="{ALLOW}" referrerpolicy="{REFERRER_POLICY}" '
        f'title="{escape(title, quote=True)}"></iframe>'
    )
