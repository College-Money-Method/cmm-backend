"""The iframe written to ``webinars.video_embed_code``.

This string is a contract in both directions: the site renders it as raw HTML,
and both `vimeo.extract_video_ref` here and `parseVimeoRef` in the front end
read the video id and privacy hash back out of it. The expected shape below was
taken from rows an admin pasted from Vimeo, so a generated replay and a
hand-added one are indistinguishable downstream.
"""

from __future__ import annotations

import re

import pytest

from src.integrations.vimeo import extract_video_ref
from src.video_pipeline.embed_code import build_embed_code, player_src

EMBED_URL = "https://player.vimeo.com/video/987654321?h=deadbeef01"


def test_the_snippet_matches_the_shape_already_in_the_table():
    code = build_embed_code("https://player.vimeo.com/video/1157544759", "Workshop #3")

    assert code == (
        '<iframe src="https://player.vimeo.com/video/1157544759?title=0&amp;byline=0'
        "&amp;portrait=0&amp;badge=0&amp;autopause=0&amp;player_id=0&amp;app_id=58479\" "
        'width="1920" height="1080" frameborder="0" '
        'allow="autoplay; fullscreen; picture-in-picture; clipboard-write; '
        'encrypted-media; web-share" '
        'referrerpolicy="strict-origin-when-cross-origin" title="Workshop #3"></iframe>'
    )


def test_the_privacy_hash_survives_and_stays_first():
    """Without the hash an embed-only video will not play at all."""
    src = player_src(EMBED_URL)

    assert src.startswith("https://player.vimeo.com/video/987654321?h=deadbeef01&")
    assert "app_id=58479" in src


def test_an_option_vimeo_already_set_is_not_overridden():
    src = player_src("https://player.vimeo.com/video/1?title=1")

    assert src.count("title=") == 1
    assert "title=1" in src


def test_the_url_comes_from_vimeo_rather_than_being_rebuilt():
    """A hand-built /video/{id}?h={hash} breaks whenever Vimeo changes the form."""
    with pytest.raises(ValueError):
        build_embed_code("", "Paying for College")


def test_a_title_with_markup_characters_cannot_break_the_attribute():
    code = build_embed_code(EMBED_URL, 'Grants & "Scholarships" <2026>')

    assert 'title="Grants &amp; &quot;Scholarships&quot; &lt;2026&gt;"' in code


def test_the_backend_parser_reads_the_reference_back_out():
    assert extract_video_ref(build_embed_code(EMBED_URL, "x")) == "987654321:deadbeef01"


# The front end parses the same string with these two patterns, copied verbatim
# from app/lib/vimeo-video-ref.ts. Kept here because a change to the option list
# or the escaping that silently stops them matching would blank the player on
# every school page with no error anywhere.
_FRONTEND_REF = re.compile(r"player\.vimeo\.com/video/(\d+)(?:\?[^\"'\s]*\bh=([0-9a-zA-Z]+))?")
_FRONTEND_OPTIONS = re.compile(r"player\.vimeo\.com/video/\d+\?([^\"'\s]*)")


def test_the_front_end_reference_pattern_still_matches():
    match = _FRONTEND_REF.search(build_embed_code(EMBED_URL, "Paying for College"))

    assert match.groups() == ("987654321", "deadbeef01")


def test_the_front_end_reads_the_player_options_after_unescaping():
    code = build_embed_code(EMBED_URL, "Paying for College")

    query = _FRONTEND_OPTIONS.search(code).group(1).replace("&amp;", "&")
    options = dict(pair.split("=", 1) for pair in query.split("&"))

    assert options["h"] == "deadbeef01"
    assert options["title"] == "0"
    assert options["app_id"] == "58479"
