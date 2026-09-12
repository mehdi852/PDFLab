"""Tests for the edit engine.

These are not ceremony: every one of them pins down a behaviour that was
established experimentally against PyMuPDF, and several of them would silently
regress into "looks fine, is wrong" territory without a check (text that is not
actually removed, artwork chewed up by the redaction, rotated pages landing in
the wrong place).
"""

from __future__ import annotations

import pymupdf
import pytest

import pdf_ops
from pdf_ops import Edit, apply_edits, collect_spans, find_text, page_sizes

TEXT_Y = 100.0
PANEL = pymupdf.Rect(20, 20, 300, 60)
PANEL_FILL = (0.9, 0.95, 1)


def build_pdf(text: str = "Hello world", *, page=True) -> bytes:
    doc = pymupdf.open()
    page_obj = doc.new_page(width=400, height=300)
    page_obj.insert_text((30, TEXT_Y), text, fontsize=14, fontname="helv")
    return doc.tobytes(garbage=3, deflate=True)


def build_colored_pdf(body: str = "In a blue box") -> bytes:
    doc = pymupdf.open()
    page_obj = doc.new_page(width=400, height=300)
    page_obj.draw_rect(PANEL, color=None, fill=PANEL_FILL)
    page_obj.insert_text((30, 45), body, fontsize=12, fontname="helv")
    page_obj.draw_line(pymupdf.Point(20, 70), pymupdf.Point(300, 70), color=(1, 0, 0), width=2)
    return doc.tobytes(garbage=3, deflate=True)


def spans_of(raw: bytes, page_index: int = 0):
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return collect_spans(doc[page_index], page_index)
    finally:
        doc.close()


def text_of(raw: bytes, page_index: int = 0) -> str:
    # Normalise non-breaking spaces so that multi-word assertions compare what a
    # reader actually sees, in both directions.
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return doc[page_index].get_text().replace("\xa0", " ")
    finally:
        doc.close()


def find_span(raw: bytes, needle: str):
    for span in spans_of(raw):
        if needle in span.text:
            return span
    raise AssertionError(f"{needle!r} not found in {text_of(raw)!r}")


def pixel_counts(raw: bytes, box: pymupdf.Rect, page_index: int = 0):
    """(dark ink pixels, colour sampled inside the box) rendered at 72dpi."""
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        pix = doc[page_index].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
        dark = 0
        for y in range(int(box.y0), int(box.y1)):
            for x in range(int(box.x0), int(box.x1)):
                r, g, b = pix.pixel(x, y)
                if r < 120 and g < 120 and b < 120:
                    dark += 1
        return dark, pix
    finally:
        doc.close()


# --------------------------------------------------------------------------
# locating text
# --------------------------------------------------------------------------

def test_spans_are_extracted_in_page_space():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    assert span.text == "Hello world"
    assert span.size == pytest.approx(14, abs=0.1)
    assert span.origin[1] == pytest.approx(TEXT_Y, abs=0.01)
    assert span.base14 == "helv"
    assert span.color == (0.0, 0.0, 0.0)


def test_find_text_under_rect_returns_the_text():
    raw = build_pdf()
    spans = spans_of(raw)
    hit = find_text(spans, pymupdf.Rect(spans[0].bbox))
    assert hit["found"] is True
    assert hit["text"] == "Hello world"
    assert hit["defaults"]["size"] == pytest.approx(14, abs=0.1)
    assert hit["defaults"]["font"] == "helv"


def test_find_text_is_off_when_rect_covers_nothing():
    raw = build_pdf()
    spans = spans_of(raw)
    hit = find_text(spans, pymupdf.Rect(30, 250, 200, 270))
    assert hit["found"] is False


def test_find_text_snaps_when_rect_is_slightly_off():
    raw = build_pdf()
    spans = spans_of(raw)
    box = pymupdf.Rect(spans[0].bbox)
    nudged = pymupdf.Rect(box.x0 + 1.0, box.y0 + 1.0, box.x1 - 1.0, box.y1 - 1.0)
    hit = find_text(spans, nudged)
    assert hit["found"] is True
    assert hit["text"] == "Hello world"


def test_a_rect_near_text_borrows_styling_but_reports_nothing_found():
    """Close to a run: the typeface is inherited, but `found` stays false.

    Reporting `found` here would make an export claim it replaced something at
    coordinates that held nothing (and would quietly reach across table rows).
    """
    raw = build_pdf()
    spans = spans_of(raw)
    # The run is bbox [30, 84.95, 99.23, 104.19]: this box sits just below it, over
    # the same columns, so it is near without touching.
    near = pymupdf.Rect(30, 108, 100, 122)
    hit = find_text(spans, near)
    assert hit["found"] is False
    assert hit["text"] == ""
    assert hit["defaults"]["from"] == "nearest"
    assert hit["defaults"]["size"] == pytest.approx(14, abs=0.2)
    assert hit["defaults"]["font"] == "helv"

    # Far off in every direction: no styling borrowed at all.
    far = pymupdf.Rect(30, 250, 200, 270)
    assert find_text(spans, far)["defaults"]["from"] == "none"


def test_borrowed_styling_is_announced_in_the_export_report():
    raw = build_pdf()
    out, report = apply_edits(raw, [Edit(page=0, rect=(30, 108, 100, 122), text="added", index=0)])
    assert report[0]["status"] == "no_text_found"
    assert any("borrowed" in w for w in report[0]["warnings"])
    assert "added" in text_of(out)
    # it picked up the nearby run's size rather than guessing from the box height
    assert report[0]["size"] == pytest.approx(14, abs=0.2)


def test_word_inside_a_longer_run_wins_over_a_stray_neighbour():
    raw = build_pdf("alpha beta, gamma")
    spans = spans_of(raw)
    line = spans[0]
    # a rect over just the middle word must report the whole run, not a fragment
    mid = pymupdf.Rect(line.bbox[0] + 40, line.bbox[1], line.bbox[0] + 75, line.bbox[3])
    hit = find_text(spans, mid)
    assert hit["found"] is True
    assert hit["text"] == "alpha beta, gamma"


# --------------------------------------------------------------------------
# replacing text
# --------------------------------------------------------------------------

def test_replace_removes_the_original_and_writes_the_new_text():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Goodbye", index=0)])

    assert report[0]["status"] == "applied"
    assert report[0]["original_text"] == "Hello world"
    assert report[0]["font"] == "Helvetica"
    assert report[0]["method"] == "text"
    after = text_of(out)
    assert "Goodbye" in after
    assert "Hello world" not in after


def ink_pixel_count(raw: bytes, box, expected, tolerance: int = 16, page_index: int = 0) -> int:
    """How many pixels in this box actually match the expected RGB colour."""
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        pix = doc[page_index].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
        count = 0
        for y in range(int(box[1]), int(box[3])):
            for x in range(int(box[0]), int(box[2])):
                pixel = pix.pixel(x, y)
                if all(abs(pixel[i] - expected[i]) <= tolerance for i in range(3)):
                    count += 1
        return count
    finally:
        doc.close()


def test_a_coloured_run_keeps_its_colour():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((40, 70), "Coloured text sample", fontsize=16, fontname="helv",
                     color=(0.8, 0.1, 0.1))
    raw = doc.tobytes()
    span = find_span(raw, "Coloured")
    assert span.color == pytest.approx((0.8, 0.1, 0.1), abs=0.01)

    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Replaced red text", index=0)])
    replacement = find_span(out, "Replaced")
    assert replacement.color == pytest.approx(span.color, abs=0.01)
    assert report[0]["color"] == "#cc1a1a"
    # and the glyphs really are painted that colour, not merely reported as such
    assert ink_pixel_count(out, replacement.bbox, (204, 26, 26)) > 40


def test_white_text_on_a_dark_panel_keeps_its_colour():
    """The light-on-dark case, which a naive "darkest pixel" preview gets wrong."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.draw_rect(pymupdf.Rect(20, 20, 380, 100), color=None, fill=(0.15, 0.18, 0.24))
    page.insert_text((40, 70), "White on dark", fontsize=16, fontname="helv", color=(1, 1, 1))
    raw = doc.tobytes()
    span = find_span(raw, "White")

    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Still white text", index=0)])
    replacement = find_span(out, "Still")
    assert replacement.color == pytest.approx((1.0, 1.0, 1.0), abs=0.01)
    assert report[0]["color"] == "#ffffff"
    assert ink_pixel_count(out, replacement.bbox, (255, 255, 255), tolerance=10) > 40
    # the dark panel behind it is still dark
    assert ink_pixel_count(out, replacement.bbox, (38, 46, 61), tolerance=14) > 40


def test_replacement_lands_on_the_original_baseline():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, _ = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Goodbye", index=0)])
    replacement = find_span(out, "Goodbye")
    assert replacement.origin[0] == pytest.approx(span.origin[0], abs=0.5)
    assert replacement.origin[1] == pytest.approx(span.origin[1], abs=0.5)
    assert replacement.size == pytest.approx(14, abs=0.2)
    # it kept the original colour too
    assert replacement.color == (0.0, 0.0, 0.0)


def test_replacement_keeps_the_original_text_style_for_a_serif_bold_run():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 100), "Serif heading", fontsize=18, fontname="tibo")
    raw = doc.tobytes()
    span = find_span(raw, "Serif")
    assert span.base14 == "tibo"
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="New heading", index=0)])
    assert report[0]["font"] == "Times-Bold"
    # The document is genuinely set in Times-Bold, one of the standard 14, so the
    # replacement reuses that exact face rather than approximating it.
    assert report[0]["font_source"] == "exact"
    assert "standard 14" in report[0]["font_note"]
    assert "New heading" in text_of(out)
    assert find_span(out, "New heading").font == span.font


def test_report_states_the_weight_the_original_run_was_set_in():
    """The weight is reported, not left for the client to guess from the name."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 100), "Bold body line", fontsize=14, fontname="hebo")
    raw = doc.tobytes()
    span = find_span(raw, "Bold body")
    assert (span.bold, span.italic) == (True, False)
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Bolder body line", index=0)])
    assert report[0]["bold"] is True
    assert report[0]["italic"] is False
    assert "Bold" in report[0]["font"]
    # ...and the exported file really is bold, not merely described as such.
    assert find_span(out, "Bolder body").bold is True


def test_italic_runs_report_and_keep_their_slant():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 100), "Slanted aside", fontsize=14, fontname="tiit")
    raw = doc.tobytes()
    span = find_span(raw, "Slanted")
    assert span.italic is True
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Slanted remark", index=0)])
    assert report[0]["italic"] is True
    assert report[0]["bold"] is False
    assert find_span(out, "Slanted remark").italic is True


def test_a_bold_flag_beats_a_font_name_that_does_not_say_bold():
    """Regression: the face choice trusted the name over the run's own flags.

    A face can be bold without saying so in its name (an embedded copy named
    plainly "Helvetica", say). Reading the name produced the regular cut and the
    replacement came out lighter than the text it replaced.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 100), "Sample", fontsize=14, fontname="helv")
    fonts = pdf_ops._FontResolver(doc)
    regular = fonts.resolve(page, 0, "Sample", name="Helvetica")
    assert regular.base14 == "helv"
    bold = fonts.resolve(page, 0, "Sample", name="Helvetica", bold=True)
    assert bold.base14 == "hebo"
    assert bold.label == "Helvetica-Bold"
    italic = fonts.resolve(page, 0, "Sample", name="Helvetica", bold=False, italic=True)
    assert italic.base14 == "heit"


def test_explicit_overrides_win():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Big", index=0, size=30.0)])
    assert report[0]["size"] == pytest.approx(30.0)
    assert find_span(out, "Big").size == pytest.approx(30.0, abs=0.3)


def test_delete_mode_removes_text_without_writing_anything():
    raw = build_pdf()
    span = find_span(raw, "world")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, mode="delete", index=0)])
    assert report[0]["status"] == "deleted"
    assert report[0]["method"] == "delete"
    assert "Hello world" not in text_of(out)


def test_empty_text_is_treated_as_a_delete():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    _, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="   ", index=0)])
    assert report[0]["method"] == "delete"


def test_partial_rect_still_removes_whole_characters():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    # cover only the left half of the run: character-level removal must still happen
    half = (span.bbox[0], span.bbox[1], span.bbox[0] + 20, span.bbox[3])
    out, _ = apply_edits(raw, [Edit(page=0, rect=half, text="X", index=0)])
    assert "Hello world" not in text_of(out)


# --------------------------------------------------------------------------
# not damaging the page
# --------------------------------------------------------------------------

def test_line_art_survives_a_replacement():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, _ = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Goodbye", index=0)])
    before = pymupdf.open(stream=raw, filetype="pdf")[0].get_drawings()
    after = pymupdf.open(stream=out, filetype="pdf")[0].get_drawings()
    assert len(after) == len(before)


def test_replacing_text_over_a_coloured_panel_does_not_paint_a_white_box():
    raw = build_colored_pdf()
    span = find_span(raw, "In a blue box")
    ink_before, _ = pixel_counts(raw, PANEL)
    out, _ = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Still the blue box", index=0)])

    doc = pymupdf.open(stream=out, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
    # sample empty space inside the panel, away from any glyphs
    assert pix.pixel(250, 30) == (229, 242, 255)
    assert pix.pixel(250, 55) == (229, 242, 255)
    ink_after, _ = pixel_counts(out, PANEL)
    assert ink_after != ink_before  # the words really changed
    assert "Still the blue box" in doc[0].get_text()


def test_a_page_with_a_vector_line_keeps_the_line_when_editing_the_same_area():
    raw = build_colored_pdf()
    span = find_span(raw, "In a blue box")
    out, _ = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Replaced", index=0)])
    doc = pymupdf.open(stream=out, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
    reds = sum(1 for x in range(20, 300) if pix.pixel(x, 70)[0] > 180 and pix.pixel(x, 70)[1] < 90)
    assert reds > 100, "the red rule under the text was destroyed"


def test_untouched_pages_are_left_alone():
    doc = pymupdf.open()
    first = doc.new_page(width=400, height=300)
    first.insert_text((30, 100), "page one", fontsize=14)
    second = doc.new_page(width=400, height=300)
    second.insert_text((30, 100), "page two", fontsize=14)
    raw = doc.tobytes()
    span = find_span(raw, "page one")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="edited", index=0)])
    assert report[0]["status"] == "applied"
    assert "page two" in text_of(out, 1)
    assert "edited" in text_of(out, 0)


def test_page_count_is_preserved():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, _ = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="x", index=0)])
    assert pymupdf.open(stream=out, filetype="pdf").page_count == 1


# --------------------------------------------------------------------------
# rotation: the coordinate contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_edits_work_at_every_rotation(rotation):
    """Span boxes are unrotated page space, so the same rect edits any rotation."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 100), "Rotated text", fontsize=14)
    page.set_rotation(rotation)
    raw = doc.tobytes()

    span = find_span(raw, "Rotated text")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Turned", index=0)])
    assert report[0]["status"] == "applied", report[0]["warnings"]
    after = text_of(out)
    assert "Turned" in after
    assert "Rotated text" not in after


def test_page_sizes_report_both_rotated_and_unrotated_geometry():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.set_rotation(90)
    sizes = page_sizes(doc)
    assert sizes[0]["width"] == 400 and sizes[0]["height"] == 200
    assert sizes[0]["display_width"] == 200 and sizes[0]["display_height"] == 400
    assert sizes[0]["rotation"] == 90


# --------------------------------------------------------------------------
# awkward input
# --------------------------------------------------------------------------

def test_non_latin_text_falls_back_to_htmlbox():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="日本語テキスト", index=0)])
    assert report[0]["status"] == "applied"
    assert report[0]["method"] == "htmlbox"
    assert "日本語" in text_of(out)


def test_latin1_accented_text_uses_the_plain_path():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    out, report = apply_edits(raw, [Edit(page=0, rect=span.bbox, text="Café Ñoño", index=0)])
    assert report[0]["method"] == "text"
    assert "Café" in text_of(out)


def test_edit_over_blank_space_warns_but_still_writes_the_text():
    """Nothing to remove means the removal can't be verified.

    The user's text is still written and the change is flagged, rather than
    silently discarded: a scanned page with no text layer is a legitimate case
    for this, and losing typed work is the worse failure.
    """
    raw = build_pdf()
    out, report = apply_edits(raw, [Edit(page=0, rect=(30, 250, 200, 270), text="ghost", index=0)])
    assert report[0]["status"] == "no_text_found"
    assert report[0]["original_text"] is None
    assert report[0]["warnings"]
    assert "ghost" in text_of(out)
    assert "Hello world" in text_of(out)


def test_out_of_range_page_and_bad_rect_are_reported():
    raw = build_pdf()
    _, report = apply_edits(raw, [
        Edit(page=9, rect=(10, 10, 50, 30), text="x", index=0),
        Edit(page=0, rect=(50, 50, 10, 10), text="y", index=1, problem="rect must be a non-empty box"),
    ])
    assert report[0]["status"] == "page_out_of_range"
    assert report[1]["status"] == "invalid"


def test_report_is_index_aligned_even_when_edits_span_pages():
    doc = pymupdf.open()
    for label in ("first page", "second page"):
        doc.new_page(width=400, height=300).insert_text((30, 100), label, fontsize=14)
    raw = doc.tobytes()
    first = find_span(raw, "first page")
    second = spans_of(raw, 1)[0]
    out, report = apply_edits(raw, [
        Edit(page=1, rect=second.bbox, text="two", index=0),
        Edit(page=0, rect=first.bbox, text="one", index=1),
    ])
    assert [r["index"] for r in report] == [0, 1]
    assert report[0]["page"] == 1 and report[1]["page"] == 0
    assert "two" in text_of(out, 1) and "one" in text_of(out, 0)


def test_multiple_edits_on_one_page_all_land():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 60), "alpha", fontsize=14)
    page.insert_text((30, 120), "beta", fontsize=14)
    page.insert_text((30, 180), "gamma", fontsize=14)
    raw = doc.tobytes()
    edits = [
        Edit(page=0, rect=find_span(raw, name).bbox, text=name.upper(), index=i)
        for i, name in enumerate(("alpha", "beta", "gamma"))
    ]
    out, report = apply_edits(raw, edits)
    assert all(r["status"] == "applied" for r in report), report
    page_text = text_of(out)
    for wanted in ("ALPHA", "BETA", "GAMMA"):
        assert wanted in page_text
    for gone in ("alpha", "beta", "gamma"):
        assert gone not in page_text


def test_export_is_idempotent_and_does_not_mutate_its_input():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    edit = Edit(page=0, rect=span.bbox, text="Once", index=0)
    first, _ = apply_edits(raw, [edit])
    second, _ = apply_edits(raw, [edit])
    assert text_of(raw) == "Hello world\n"          # original untouched
    assert text_of(first) == text_of(second)


def test_autofit_shrinks_text_to_the_original_run_width():
    raw = build_pdf()
    span = find_span(raw, "Hello")
    long_text = "Goodbye world and all who sail in her"
    out, report = apply_edits(raw, [
        Edit(page=0, rect=span.bbox, text=long_text, index=0, autofit=True)])
    assert report[0]["size"] < 14
    assert report[0]["size"] >= 14 * 0.45
    assert long_text in text_of(out)


def test_pad_expands_the_removal_box():
    raw = build_pdf()
    span = find_span(raw, "world")
    tight = Edit(page=0, rect=span.bbox, text="planet", index=0, pad=6.0)
    out, _ = apply_edits(raw, [tight])
    assert "Hello world" not in text_of(out)


def test_edits_on_a_page_with_no_text_at_all_do_not_crash():
    doc = pymupdf.open()
    doc.new_page(width=200, height=200)
    raw = doc.tobytes()
    out, report = apply_edits(raw, [Edit(page=0, rect=(10, 10, 60, 40), text="x", index=0)])
    assert report[0]["status"] == "no_text_found"
    assert pymupdf.open(stream=out, filetype="pdf").page_count == 1


def test_no_edits_returns_a_readable_pdf():
    raw = build_pdf()
    out, report = apply_edits(raw, [])
    assert report == []
    assert "Hello world" in text_of(out)


def test_garbage_input_is_rejected():
    with pytest.raises(pdf_ops.InvalidDocument):
        pdf_ops.open_document(b"not a pdf at all")
