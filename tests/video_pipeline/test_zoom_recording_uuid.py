"""Zoom recording UUID encoding.

Recording UUIDs are base64 and roughly one in eight begins with `/` or contains
`//`. Passed raw into a path, those collapse the URL and Zoom answers 404 for a
recording that exists — a silent, intermittent loss of whole webinars. Zoom's
API docs require the id to be double URL-encoded in that case.
"""

from __future__ import annotations

from src.integrations.zoom import encode_recording_uuid


def test_leading_slash_is_double_encoded():
    assert encode_recording_uuid("/abc123==") == "%252Fabc123%253D%253D"


def test_embedded_double_slash_is_double_encoded():
    assert encode_recording_uuid("ab//cd==") == "ab%252F%252Fcd%253D%253D"


def test_clean_uuid_survives_double_encoding():
    """Encoding twice unconditionally is safe — Zoom decodes twice either way."""
    encoded = encode_recording_uuid("aBcDeFgHiJk")
    assert encoded == "aBcDeFgHiJk"


def test_encoding_leaves_no_path_separator():
    """The property that actually matters: nothing can escape its path segment."""
    for raw in ("/abc==", "ab//cd==", "a+b/c=", "///", "abc"):
        assert "/" not in encode_recording_uuid(raw)


def test_result_decodes_back_to_the_original():
    """Double-encoded once by us, decoded twice by Zoom's gateway and API."""
    from urllib.parse import unquote

    for raw in ("/abc==", "ab//cd==", "a+b/c=", "abc"):
        assert unquote(unquote(encode_recording_uuid(raw))) == raw


def test_plus_is_encoded_not_left_to_become_a_space():
    """`+` is valid base64 but decodes to a space in a query string."""
    assert "+" not in encode_recording_uuid("a+b/c=")
