"""Unit tests for link/merge-tag resolution (ported from merge-tag-utils.ts)."""

from src.emails.link_resolver import (
    build_internal_link_href,
    resolve_marks,
    resolve_merge_tag,
    resolve_plain_text,
    resolve_tiptap_doc,
)


class TestBuildInternalLinkHref:
    def test_with_origin_and_school_slug(self):
        href = build_internal_link_href(
            "/topic/fafsa-basics", origin="https://next.collegemoneymethod.com", school_slug="lincoln-high"
        )
        assert href == "https://next.collegemoneymethod.com/school/lincoln-high/topic/fafsa-basics"

    def test_without_school_slug(self):
        href = build_internal_link_href("/resources/42", origin="https://next.collegemoneymethod.com", school_slug=None)
        assert href == "https://next.collegemoneymethod.com/resources/42"

    def test_without_origin_stays_relative(self):
        href = build_internal_link_href("/topic/x", origin=None, school_slug="lincoln-high")
        assert href == "/school/lincoln-high/topic/x"

    def test_empty_path_passes_through(self):
        assert build_internal_link_href("", origin="https://x.com", school_slug="s") == ""


class TestResolvePlainText:
    def test_substitutes_known_tag(self):
        assert resolve_plain_text("Hi {{name}}!", {"name": "Jordan"}) == "Hi Jordan!"

    def test_unknown_tag_passes_through(self):
        assert resolve_plain_text("Hi {{name}}!", {}) == "Hi {{name}}!"

    def test_multiple_tags(self):
        result = resolve_plain_text("{{a}} and {{b}}", {"a": "1", "b": "2"})
        assert result == "1 and 2"


class TestResolveMarks:
    def test_legacy_internal_link_mark_converts_to_absolute_link(self):
        marks = [{"type": "internalLink", "attrs": {"href": "/topic/x"}}]
        resolved = resolve_marks(marks, {}, origin="https://next.collegemoneymethod.com", school_slug="lincoln-high")
        assert resolved == [
            {
                "type": "link",
                "attrs": {
                    "href": "https://next.collegemoneymethod.com/school/lincoln-high/topic/x",
                    "target": "_blank",
                    "rel": "noopener noreferrer nofollow",
                },
            }
        ]

    def test_internal_link_path_resolves_absolute(self):
        marks = [{"type": "link", "attrs": {"href": "/resources/7"}}]
        resolved = resolve_marks(marks, {}, origin="https://x.com", school_slug=None)
        assert resolved[0]["attrs"]["href"] == "https://x.com/resources/7"
        assert resolved[0]["attrs"]["target"] == "_blank"

    def test_template_variable_href_substituted(self):
        marks = [{"type": "link", "attrs": {"href": "{{resource_center_url}}"}}]
        resolved = resolve_marks(marks, {"resource_center_url": "https://x.com/rc"}, origin=None, school_slug=None)
        assert resolved[0]["attrs"]["href"] == "https://x.com/rc"

    def test_external_link_passes_through_unchanged(self):
        marks = [{"type": "link", "attrs": {"href": "https://zoom.us/j/1"}}]
        resolved = resolve_marks(marks, {}, origin=None, school_slug=None)
        assert resolved == marks

    def test_non_link_marks_untouched(self):
        marks = [{"type": "bold"}, {"type": "italic"}]
        assert resolve_marks(marks, {}, origin=None, school_slug=None) == marks


class TestResolveMergeTag:
    def test_simple_value(self):
        node = {"type": "mergeTag", "attrs": {"tag": "first_name"}}
        nodes = resolve_merge_tag(node, {"first_name": "Jordan"})
        assert nodes == [{"type": "text", "text": "Jordan"}]

    def test_missing_tag_passthrough(self):
        node = {"type": "mergeTag", "attrs": {"tag": "missing"}}
        nodes = resolve_merge_tag(node, {})
        assert nodes == [{"type": "text", "text": "{{missing}}"}]

    def test_bare_url_line_is_linkified(self):
        node = {"type": "mergeTag", "attrs": {"tag": "link_tag"}}
        nodes = resolve_merge_tag(node, {"link_tag": "https://example.com/a"})
        assert nodes == [
            {
                "type": "text",
                "text": "https://example.com/a",
                "marks": [
                    {
                        "type": "link",
                        "attrs": {
                            "href": "https://example.com/a",
                            "target": "_blank",
                            "rel": "noopener noreferrer nofollow",
                        },
                    }
                ],
            }
        ]

    def test_multiline_value_expands_with_hard_breaks(self):
        node = {"type": "mergeTag", "attrs": {"tag": "list_tag"}}
        value = "- Name (https://example.com/b)\nPlain line"
        nodes = resolve_merge_tag(node, {"list_tag": value})
        assert nodes[0] == {"type": "text", "text": "- "}
        assert nodes[1]["text"] == "Name"
        assert nodes[1]["marks"][0]["attrs"]["href"] == "https://example.com/b"
        assert nodes[2] == {"type": "hardBreak"}
        assert nodes[3] == {"type": "text", "text": "Plain line"}

    def test_chip_marks_carry_onto_replacement_text(self):
        node = {
            "type": "mergeTag",
            "attrs": {"tag": "first_name"},
            "marks": [{"type": "bold"}, {"type": "italic"}],
        }
        nodes = resolve_merge_tag(node, {"first_name": "Jordan"})
        assert nodes == [
            {"type": "text", "text": "Jordan", "marks": [{"type": "bold"}, {"type": "italic"}]}
        ]

    def test_chip_marks_carry_onto_every_line_but_not_hard_breaks(self):
        node = {"type": "mergeTag", "attrs": {"tag": "list_tag"}, "marks": [{"type": "bold"}]}
        nodes = resolve_merge_tag(node, {"list_tag": "One\nTwo"})
        assert nodes == [
            {"type": "text", "text": "One", "marks": [{"type": "bold"}]},
            {"type": "hardBreak"},
            {"type": "text", "text": "Two", "marks": [{"type": "bold"}]},
        ]

    def test_line_generated_link_wins_over_inherited_link(self):
        node = {
            "type": "mergeTag",
            "attrs": {"tag": "link_tag"},
            "marks": [{"type": "link", "attrs": {"href": "https://outer.example"}}, {"type": "bold"}],
        }
        nodes = resolve_merge_tag(node, {"link_tag": "https://example.com/a"})
        assert nodes[0]["marks"][0]["attrs"]["href"] == "https://example.com/a"
        assert {"type": "bold"} in nodes[0]["marks"]


class TestResolveTiptapDoc:
    def test_resolves_merge_tag_inline_within_paragraph(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "mergeTag", "attrs": {"tag": "name"}},
                        {"type": "text", "text": "!"},
                    ],
                }
            ],
        }
        resolved = resolve_tiptap_doc(doc, {"name": "Jordan"})
        assert resolved["content"][0]["content"] == [
            {"type": "text", "text": "Jordan"},
            {"type": "text", "text": "!"},
        ]

    def test_internal_link_on_a_chip_is_resolved_before_it_is_inherited(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mergeTag",
                            "attrs": {"tag": "name"},
                            "marks": [{"type": "link", "attrs": {"href": "/topic/x"}}],
                        }
                    ],
                }
            ],
        }
        resolved = resolve_tiptap_doc(doc, {"name": "Jordan"}, origin="https://x.com", school_slug="lincoln-high")
        node = resolved["content"][0]["content"][0]
        assert node["text"] == "Jordan"
        assert node["marks"][0]["attrs"]["href"] == "https://x.com/school/lincoln-high/topic/x"
