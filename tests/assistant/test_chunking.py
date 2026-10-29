"""Tiptap/HTML -> heading sections -> bounded chunks."""

from __future__ import annotations

import json

from src.assistant.chunking import Section, chunk_sections, plain_text, rich_text_sections


def _doc(*nodes):
    return json.dumps({"type": "doc", "content": list(nodes)})


def _p(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _h(text):
    return {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": text}]}


def test_sections_split_at_headings():
    sections = rich_text_sections(_doc(_p("Intro."), _h("Divorced parents"), _p("Custodial parent files.")))
    assert sections == [Section(None, "Intro."), Section("Divorced parents", "Custodial parent files.")]


def test_list_items_become_lines_and_inline_marks_do_not_add_spaces():
    bullet = {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_p("Tuition")]},
            {"type": "listItem", "content": [_p("Housing")]},
        ],
    }
    marked = {"type": "paragraph", "content": [{"type": "text", "text": "Net"}, {"type": "text", "text": "Price"}]}
    assert plain_text(_doc(bullet, marked)) == "Tuition\nHousing\nNetPrice"


def test_html_keeps_breaks_and_decodes_entities():
    assert plain_text("<ul><li>Merit &amp; need</li><li>Loans</li></ul>") == "Merit & need\nLoans"


def test_plain_and_numeric_strings_are_text():
    assert plain_text("2024") == "2024"
    assert rich_text_sections("Just text", default_heading="Key actions") == [Section("Key actions", "Just text")]


def test_empty_values():
    assert plain_text(None) == ""
    assert rich_text_sections("") == []


def test_long_section_splits_under_limit_on_sentences():
    text = " ".join(f"Sentence number {i} explains aid." for i in range(100))
    chunks = chunk_sections([Section("H", text)], max_chars=300, min_chars=50)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)
    assert all(c.heading == "H" for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)


def test_short_sections_merge_into_neighbour_with_heading_label():
    chunks = chunk_sections([Section(None, "Short intro."), Section("FAFSA", "Fill it in October.")], min_chars=300)
    assert len(chunks) == 1
    assert chunks[0].text == "Short intro.\nFAFSA: Fill it in October."


def test_long_sections_stay_separate():
    a, b = "a" * 400, "b" * 400
    chunks = chunk_sections([Section("A", a), Section("B", b)], max_chars=1800, min_chars=300)
    assert [(c.heading, c.text) for c in chunks] == [("A", a), ("B", b)]


def test_index_hash_changes_with_content_and_build_settings():
    import uuid

    from src.assistant.sources import SourceDocument
    from src.assistant.sync import index_hash

    doc = SourceDocument("faq", uuid.uuid4(), "Q", None, [Section(None, "A")])
    assert index_hash(doc, True) == index_hash(doc, True)
    assert index_hash(doc, True) != index_hash(doc, False)
    edited = SourceDocument("faq", doc.source_id, "Q", None, [Section(None, "A, revised")])
    assert index_hash(edited, True) != index_hash(doc, True)
