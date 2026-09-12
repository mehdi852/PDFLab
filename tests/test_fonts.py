"""Tests for putting the right typeface on a replacement.

Three behaviours matter here, in order of how badly they would hurt if they
regressed:

1. The document's own font is reused whenever that is possible.
2. A font that cannot be reused is replaced with the closest thing available,
   never with a different family's face and never with a silent blank.
3. A font whose character map was stripped by subsetting must NOT be used: it
   accepts the call and emits null bytes, which is silent data corruption.
"""

from __future__ import annotations

import pymupdf
import pytest

from font_matching import (css_family_stack, display_family, family_key, name_key,
                           style_of, system_fonts)
from pdf_ops import Edit, apply_edits, collect_spans


@pytest.fixture(scope="module")
def serif_font_path() -> str:
    """A real serif face installed on this machine."""
    for font in system_fonts.scan():
        if font.kind == "serif" and not font.bold and not font.italic:
            return font.path
    pytest.skip("no suitable serif font installed")


def build_with_embedded(path: str, *, subset: bool, body: bool = True) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    page.insert_text((40, 100), "Embedded sample text", fontsize=16, fontname="EM", fontfile=path)
    if body:
        for i in range(5):
            page.insert_text((40, 140 + i * 20), f"Body line {i} in the same face",
                             fontsize=11, fontname="EM")
    if subset:
        doc.subset_fonts()
    return doc.tobytes(garbage=3, deflate=True)


def span_of(raw: bytes, needle: str):
    for span in collect_spans(pymupdf.open(stream=raw, filetype="pdf")[0], 0):
        if needle in span.text:
            return span
    raise AssertionError(f"{needle!r} not found")


def text_of(raw: bytes) -> str:
    # MuPDF reports spaces as non-breaking when text is drawn in an embedded
    # font, so normalise before comparing.
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return doc[0].get_text().replace("\xa0", " ")
    finally:
        doc.close()


# --------------------------------------------------------------------------
# the closest face available
# --------------------------------------------------------------------------

def test_family_key_normalises_the_ways_pdfs_name_fonts():
    assert family_key("HSYXQO+Georgia Regular") == "georgia"
    assert family_key("Arial-BoldMT") == "arial"
    assert family_key("ArialMT") == "arial"
    assert family_key("Helvetica-Oblique") == "helvetica"
    assert family_key("BCDEEE+Calibri") == "calibri"
    assert family_key("Courier New Bold Italic") == "couriernew"
    # "Roman" belongs to the family here, but is a style word in "Times-Roman".
    assert family_key("Times New Roman Regular") == "timesnewroman"
    assert family_key("Times-Roman") == "timesroman"
    assert family_key("") == ""


def test_name_key_keeps_the_style_so_bold_finds_bold():
    assert name_key("HSYXQO+Georgia Regular") == "georgiaregular"
    assert name_key("Arial-BoldMT") == "arialboldmt"
    assert name_key("ArialMT") != name_key("Arial-BoldMT")
    assert style_of("Arial-BoldMT") == (True, False)
    assert style_of("Times New Roman Italic") == (False, True)


def test_display_family_is_something_a_browser_can_name():
    assert display_family("Cambria-Bold") == "Cambria"
    assert display_family("HSYXQO+Georgia Regular") == "Georgia"
    assert display_family("Times New Roman Regular") == "Times New Roman"
    assert display_family("SegoeUI") == "SegoeUI"


def test_css_stack_names_the_family_and_ends_in_a_generic():
    serif = css_family_stack("Cambria-Bold", "serif")
    assert '"Cambria"' in serif
    assert serif.endswith("serif")
    assert css_family_stack("Courier New", "mono").endswith("monospace")
    assert css_family_stack("Whatever", "sans").endswith("sans-serif")
    # a serif document should never preview in a sans stack
    assert not css_family_stack("Georgia", "serif").endswith("sans-serif")


def test_matching_picks_the_same_family_when_it_is_installed():
    available = {f.family for f in system_fonts.scan()}
    if "georgia" not in available:
        pytest.skip("Georgia is not installed here")
    match = system_fonts.best_match("Georgia Regular", "Replacement text")
    assert match is not None
    assert family_key(match.name).startswith("georgia")


def test_matching_respects_bold_and_italic():
    match = system_fonts.best_match("Georgia Bold", "Bold replacement")
    if match is None:
        pytest.skip("no bold face available")
    assert match.bold is True
    italic = system_fonts.best_match("Georgia Italic", "Italic replacement")
    if italic is not None:
        assert italic.italic is True


def test_an_unknown_family_still_gets_a_sensible_face():
    match = system_fonts.best_match("TotallyMadeUpFamily-Bold", "Some replacement text")
    if match is None:
        pytest.skip("no fonts installed")
    assert match.bold is True  # it honoured the style even without knowing the family


def test_icon_and_symbol_fonts_are_never_selected():
    """Dingbat faces cover the Latin range with pictures, so coverage alone lies."""
    for font in system_fonts.scan():
        lowered = font.name.lower()
        assert not any(h in lowered for h in ("icon", "dingbat", "webding", "wingding", "mdl2")), font.name


def test_a_face_is_never_returned_unless_it_can_draw_the_text():
    """Whatever comes back must genuinely cover every character."""
    match = system_fonts.best_match("Georgia Regular", "日本語のテキスト")
    if match is None:
        return  # declining is fine; the caller falls back to MuPDF's own font
    parsed = pymupdf.Font(fontfile=match.path)
    assert all(parsed.has_glyph(ord(ch)) for ch in "日本語"), match.name


# --------------------------------------------------------------------------
# reuse: the document's own font
# --------------------------------------------------------------------------

def test_embedded_font_is_reused_exactly(serif_font_path):
    raw = build_with_embedded(serif_font_path, subset=False)
    span = span_of(raw, "Embedded")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Rewritten heading", index=0)])

    assert report[0]["status"] == "applied"
    assert report[0]["font_source"] == "exact", report[0]
    assert "document's own embedded font" in report[0]["font_note"]
    text = text_of(out)
    assert "Rewritten heading" in text
    assert "Embedded sample text" not in text
    # the replacement is drawn in the same family as the original run
    assert family_key(span_of(out, "Rewritten").font) == family_key(span.font)
    # and the rest of the page is untouched
    for i in range(5):
        assert f"Body line {i}" in text


def test_reusing_a_font_does_not_bloat_the_document(serif_font_path):
    raw = build_with_embedded(serif_font_path, subset=False)
    span = span_of(raw, "Embedded")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Tiny change", index=0)])
    assert report[0]["font_source"] == "exact"
    # A whole font dropped into a small document would balloon it; subsetting
    # after the edit keeps the result in the same league as the original.
    assert len(out) <= len(raw) * 1.5 + 10_000, f"{len(out)} vs original {len(raw)}"


def test_a_standard_font_document_is_reported_as_exact():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((40, 100), "Plain Helvetica text", fontsize=14, fontname="helv")
    raw = doc.tobytes()
    span = span_of(raw, "Plain")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="New plain text", index=0)])
    assert report[0]["font_source"] == "exact"
    assert report[0]["font"] == "Helvetica"
    assert span_of(out, "New").size == pytest.approx(14, abs=0.2)


# --------------------------------------------------------------------------
# the corruption guard
# --------------------------------------------------------------------------

def test_a_stripped_subset_font_never_writes_garbage(serif_font_path):
    """Subsetting can remove the character map entirely.

    Such a font accepts insert_text and silently produces null bytes, so every
    candidate is render-tested first and rejected if it cannot spell the text.
    """
    raw = build_with_embedded(serif_font_path, subset=True)
    span = span_of(raw, "Embedded")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Readable replacement", index=0)])

    text = text_of(out)
    assert "\x00" not in text, "a font that cannot spell the text was used anyway"
    assert "Readable replacement" in text
    assert report[0]["status"] in ("applied", "written_unverified")
    assert report[0]["font_source"] in ("matched", "approximate")
    assert report[0]["font_note"]


def test_text_added_next_to_a_paragraph_borrows_its_typeface(serif_font_path):
    raw = build_with_embedded(serif_font_path, subset=False)
    near = span_of(raw, "Body line 4")   # the last line, so nothing sits below it
    # clear of the run itself, so there is nothing here to remove
    rect = (near.bbox[0], near.bbox[3] + 5, near.bbox[2], near.bbox[3] + 15)
    out, report = apply_edits(raw, [Edit(page=0, rect=rect, text="Added line", index=0)])
    assert report[0]["status"] == "no_text_found"
    assert "Added line" in text_of(out)
    assert report[0]["font_source"] in ("exact", "matched")


def test_explicit_font_request_wins(serif_font_path):
    raw = build_with_embedded(serif_font_path, subset=False)
    span = span_of(raw, "Embedded")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Forced face",
                                         font="tiro", index=0)])
    assert report[0]["font"] == "Times-Roman"
    assert report[0]["font_source"] == "requested"
    assert "Forced face" in text_of(out)
