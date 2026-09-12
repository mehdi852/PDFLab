"""Tests for the markup layer: pen, arrows, shapes, highlights, text boxes, images.

The failures worth catching here are the silent ones -- a markup painted before
the redaction pass and then wiped, a highlight that is opaque when it should be
translucent, an image call that adds nothing to the page. So these tests render
the exported bytes and count pixels instead of trusting the drawing call.

Colours are asserted through the *rendered* page, not the report: a report that
says "#cc1a1a" and a page with no red on it is exactly the bug this file exists
to find.
"""

from __future__ import annotations

import base64

import pymupdf
import pytest

import pdf_ops
from pdf_ops import Annotation, Edit, apply_edits

RED = (0.8, 0.1, 0.1)
BLUE = (0.1, 0.3, 0.8)
YELLOW = (1.0, 1.0, 0.0)
GREEN = (0.2, 0.7, 0.35)


def build_pdf(*lines: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    for index, line in enumerate(lines or ("Hello world",)):
        page.insert_text((30, 40 + index * 20), line, fontsize=12, fontname="helv")
    return doc.tobytes(garbage=3, deflate=True)


def png_bytes(color=GREEN, size: int = 40) -> bytes:
    """A real PNG, built the same way a browser would hand one over."""
    src = pymupdf.open()
    page = src.new_page(width=size, height=size)
    page.draw_rect(pymupdf.Rect(0, 0, size, size), color=None, fill=color, width=0)
    data = page.get_pixmap(dpi=36).tobytes("png")
    src.close()
    return data


def raw_pixmap(raw: bytes, page_index: int = 0):
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return doc[page_index].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
    finally:
        doc.close()


def pixel(pix, x: float, y: float):
    return pix.pixel(int(x), int(y))


def count_near(pix, rgb, tol: int = 30) -> int:
    hits = 0
    for y in range(pix.height):
        for x in range(pix.width):
            px = pix.pixel(x, y)
            if all(abs(px[i] - rgb[i]) <= tol for i in range(3)):
                hits += 1
    return hits


def as_255(rgb) -> tuple[int, int, int]:
    return tuple(round(c * 255) for c in rgb)  # type: ignore[return-value]


def text_of(raw: bytes, page_index: int = 0) -> str:
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return doc[page_index].get_text().replace("\xa0", " ")
    finally:
        doc.close()


def page_count(raw: bytes) -> int:
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def rows(report, kind: str):
    return [row for row in report if row.get("kind") == kind]


# --------------------------------------------------------------------------
# highlights
# --------------------------------------------------------------------------

def test_highlight_is_drawn_and_stays_translucent():
    """A highlighter must tint what is under it, not paint over it.

    Rendered on white, a 35% yellow fill lands near (255, 255, 166). An opaque
    fill would give (255, 255, 0), which is the regression this pins down.
    """
    rect = (30.0, 25.0, 180.0, 45.0)
    ann = Annotation.from_dict({
        "kind": "highlight", "page": 0, "rect": list(rect), "color": YELLOW,
    })
    out, report = apply_edits(build_pdf("Highlight me"), [], [ann])

    assert report[0]["status"] == "applied"
    assert report[0]["color"] == "#ffff00"
    pix = raw_pixmap(out)
    sample = pixel(pix, 100, 35)
    assert sample[0] > 240 and sample[1] > 240, sample
    assert 130 < sample[2] < 215, f"highlight was not translucent: {sample}"


def test_highlight_keeps_the_text_under_it_readable():
    ann = Annotation.from_dict({
        "kind": "highlight", "page": 0, "rect": [30.0, 25.0, 180.0, 45.0],
        "color": YELLOW,
    })
    out, _ = apply_edits(build_pdf("Highlight me"), [], [ann])
    assert "Highlight me" in text_of(out)


# --------------------------------------------------------------------------
# ink, lines and arrows
# --------------------------------------------------------------------------

def test_freehand_ink_renders_in_its_colour():
    points = [[40.0 + i * 6, 120.0 + (i % 3) * 8] for i in range(24)]
    ann = Annotation.from_dict({
        "kind": "ink", "page": 0, "points": points, "color": RED, "width": 3,
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    assert count_near(raw_pixmap(out), as_255(RED), tol=60) > 100
    assert len(pymupdf.open(stream=out, filetype="pdf")[0].get_drawings()) >= 1


def test_ink_needs_two_points():
    ann = Annotation.from_dict({"kind": "ink", "page": 0, "points": [[10.0, 10.0]]})
    assert ann.problem is not None
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"
    assert "two points" in report[0]["warnings"][0]


def test_arrow_gets_a_filled_head_at_the_tip():
    """A shaft is a line; the head is the whole point of an arrow.

    Sampled a few points back from the tip, across the arrow's axis: a bare line
    is thin there, the head's triangle is not.
    """
    ann = Annotation.from_dict({
        "kind": "arrow", "page": 0, "points": [[40.0, 200.0], [240.0, 200.0]],
        "color": RED, "width": 3,
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    pix = raw_pixmap(out)
    thick = sum(1 for dy in range(-6, 7) if sum(pixel(pix, 232, 200 + dy)) < 700)
    shaft = sum(1 for dy in range(-6, 7) if sum(pixel(pix, 140, 200 + dy)) < 700)
    assert thick > shaft + 3, f"no arrowhead: tip {thick}px vs shaft {shaft}px"


def test_a_degenerate_arrow_does_not_raise():
    ann = Annotation.from_dict({
        "kind": "arrow", "page": 0, "points": [[40.0, 200.0], [40.0, 200.0]],
        "color": RED,
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    assert page_count(out) == 1


def test_opacity_reaches_the_exported_stroke():
    """50% red on white renders pink; a fully opaque stroke would not."""
    ann = Annotation.from_dict({
        "kind": "line", "page": 0, "points": [[40.0, 250.0], [300.0, 250.0]],
        "color": RED, "width": 6, "opacity": 0.4,
    })
    out, _ = apply_edits(build_pdf(), [], [ann])
    sample = pixel(raw_pixmap(out), 150, 250)
    assert sample[0] > 200, f"stroke should be faded towards the page: {sample}"


# --------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["rect", "ellipse"])
def test_shapes_draw_an_outline(kind):
    ann = Annotation.from_dict({
        "kind": kind, "page": 0, "rect": [60.0, 120.0, 220.0, 220.0],
        "color": BLUE, "width": 2,
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    assert count_near(raw_pixmap(out), as_255(BLUE), tol=60) > 50


def test_shape_fill_is_optional_and_off_by_default():
    empty = Annotation.from_dict({
        "kind": "rect", "page": 0, "rect": [60.0, 120.0, 220.0, 220.0], "color": BLUE,
    })
    filled = Annotation.from_dict({
        "kind": "rect", "page": 0, "rect": [60.0, 120.0, 220.0, 220.0], "color": BLUE,
        "fill_opacity": 0.2,
    })
    plain, _ = apply_edits(build_pdf(), [], [empty])
    washed, _ = apply_edits(build_pdf(), [], [filled])
    assert pixel(raw_pixmap(plain), 140, 170) == (255, 255, 255)
    assert pixel(raw_pixmap(washed), 140, 170) != (255, 255, 255)


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

def test_image_is_inserted_and_lands_in_its_box():
    ann = Annotation.from_dict({
        "kind": "image", "page": 0, "rect": [250.0, 150.0, 350.0, 250.0],
        "image": {"data": base64.b64encode(png_bytes()).decode(), "mime": "image/png"},
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    doc = pymupdf.open(stream=out, filetype="pdf")
    try:
        assert len(doc[0].get_images(full=True)) == 1
    finally:
        doc.close()
    assert count_near(raw_pixmap(out), as_255(GREEN), tol=60) > 2000


def test_image_without_data_is_rejected_not_crashed():
    ann = Annotation.from_dict({"kind": "image", "page": 0, "rect": [10.0, 10.0, 90.0, 90.0]})
    assert ann.problem is not None
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"
    assert page_count(out) == 1


def test_undecodable_image_data_is_reported():
    ann = Annotation.from_dict({
        "kind": "image", "page": 0, "rect": [10.0, 10.0, 90.0, 90.0],
        "image": {"data": "not base64 at all !!"},
    })
    assert ann.problem is not None
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"


# --------------------------------------------------------------------------
# new text boxes
# --------------------------------------------------------------------------

def test_text_box_writes_readable_text_in_the_requested_colour():
    ann = Annotation.from_dict({
        "kind": "text", "page": 0, "rect": [40.0, 100.0, 260.0, 120.0],
        "text": "Added by the markup bar", "color": RED, "size": 13, "font": "hebo",
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    assert "Added by the markup bar" in text_of(out)
    assert report[0]["font_source"] == "requested"
    assert count_near(raw_pixmap(out), as_255(RED), tol=60) > 30


def test_text_box_grows_instead_of_dropping_the_text():
    """insert_textbox writes nothing when the text does not fit, so a long line
    in a short box must grow the box rather than silently vanish."""
    ann = Annotation.from_dict({
        "kind": "text", "page": 0, "rect": [40.0, 100.0, 140.0, 112.0],
        "text": "A fairly long sentence that cannot possibly fit in this tiny box.",
        "size": 12, "font": "helv",
    })
    out, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "applied"
    assert "tiny box" in text_of(out).replace("\n", " ")


def test_centred_text_box_is_actually_centred():
    ann = Annotation.from_dict({
        "kind": "text", "page": 0, "rect": [40.0, 100.0, 360.0, 130.0],
        "text": "centred", "size": 16, "align": 1, "font": "hebo",
    })
    out, _ = apply_edits(build_pdf(), [], [ann])
    doc = pymupdf.open(stream=out, filetype="pdf")
    try:
        boxes = [span["bbox"] for block in doc[0].get_text("dict")["blocks"]
                 for line in block.get("lines", ()) for span in line.get("spans", ())
                 if span["text"].strip() == "centred"]
    finally:
        doc.close()
    assert boxes, "the centred text is not on the page"
    middle = (boxes[0][0] + boxes[0][2]) / 2
    assert abs(middle - 200) < 3, f"centre of 40..360 is 200, span sits at {middle}"


def test_empty_text_box_draws_nothing_and_says_so():
    ann = Annotation.from_dict({
        "kind": "text", "page": 0, "rect": [40.0, 100.0, 200.0, 120.0], "text": "   ",
    })
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"
    assert "empty" in report[0]["warnings"][0]


def test_text_box_can_match_the_document():
    """font=null means \"match the surrounding text\", not \"pick something\"."""
    ann = Annotation.from_dict({
        "kind": "text", "page": 0, "rect": [30.0, 55.0, 300.0, 75.0],
        "text": "Beside the paragraph", "size": 12,
    })
    out, report = apply_edits(build_pdf("Hello world"), [], [ann])
    assert report[0]["status"] == "applied"
    assert report[0]["font_source"] in ("exact", "matched", "approximate")
    assert "Beside the paragraph" in text_of(out)


# --------------------------------------------------------------------------
# how markups sit next to the text editor
# --------------------------------------------------------------------------

def test_markup_survives_on_a_page_that_also_has_a_text_edit():
    """The redaction pass rewrites the content stream; a markup must be painted
    after it or it is wiped from the page with no error anywhere."""
    raw = build_pdf("Replace this line")
    spans = pdf_ops.collect_spans(pymupdf.open(stream=raw, filetype="pdf")[0], 0)
    edit = Edit(page=0, rect=spans[0].bbox, text="Replaced line", index=0)
    ann = Annotation.from_dict({
        "kind": "highlight", "page": 0, "rect": [30.0, 25.0, 200.0, 45.0],
        "color": YELLOW,
    }, index=1)
    out, report = apply_edits(raw, [edit], [ann])

    body = text_of(out)
    assert "Replaced line" in body and "Replace this line" not in body
    assert report[1]["status"] == "applied", report[1]
    # Sampled clear of the replaced glyphs, so the pixel is the highlighter alone.
    sample = pixel(raw_pixmap(out), 170, 35)
    assert 135 < sample[2] < 215, f"highlight was wiped by the redaction: {sample}"


def test_a_markup_needs_no_text_edit_on_its_page():
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    raw = doc.tobytes(garbage=3, deflate=True)

    ann = Annotation.from_dict({
        "kind": "rect", "page": 1, "rect": [60.0, 60.0, 200.0, 140.0], "color": BLUE,
    })
    out, report = apply_edits(raw, [], [ann])
    assert page_count(out) == 2
    assert len(report) == 1
    assert (report[0]["status"], report[0]["page"], report[0]["method"]) == ("applied", 1, "rect")
    assert count_near(raw_pixmap(out, 0), as_255(BLUE), tol=60) == 0, "page 1 was drawn on"
    assert count_near(raw_pixmap(out, 1), as_255(BLUE), tol=60) > 50


def test_markup_on_a_missing_page_is_reported():
    ann = Annotation.from_dict({
        "kind": "rect", "page": 7, "rect": [10.0, 10.0, 60.0, 60.0], "color": BLUE,
    })
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "page_out_of_range"


def test_markup_off_the_page_is_reported_rather_than_crashed():
    ann = Annotation.from_dict({
        "kind": "rect", "page": 0, "rect": [500.0, 400.0, 560.0, 460.0], "color": BLUE,
    })
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"
    assert "outside the page" in report[0]["warnings"][0]


def test_unknown_markup_kind_is_reported():
    ann = Annotation.from_dict({"kind": "sparkle", "page": 0, "rect": [10.0, 10.0, 60.0, 60.0]})
    assert "sparkle" in ann.problem
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["status"] == "invalid"


# --------------------------------------------------------------------------
# the report the client audits
# --------------------------------------------------------------------------

def test_report_carries_one_row_per_edit_and_one_per_markup():
    raw = build_pdf("Replace this line")
    spans = pdf_ops.collect_spans(pymupdf.open(stream=raw, filetype="pdf")[0], 0)
    edit = Edit(page=0, rect=spans[0].bbox, text="Replaced", index=0)
    anns = [
        Annotation.from_dict({"kind": "arrow", "page": 0,
                              "points": [[10.0, 280.0], [90.0, 280.0]]}, index=1),
        Annotation.from_dict({"kind": "highlight", "page": 0,
                              "rect": [30.0, 25.0, 180.0, 45.0], "color": YELLOW}, index=2),
    ]
    _, report = apply_edits(raw, [edit], anns)
    assert [row["index"] for row in report] == [0, 1, 2]
    assert rows(report, "annotation") == [report[1], report[2]]
    assert report[1]["label"] == "Arrow"
    assert report[2]["label"] == "Highlight"
    assert report[2]["detail"].startswith("150×20pt")
    assert report[2]["color"] == "#ffff00"


def test_annotation_index_is_kept_so_the_client_can_map_rows_back():
    ann = Annotation.from_dict({"kind": "highlight", "page": 0,
                                "rect": [10.0, 10.0, 60.0, 40.0]}, index=41)
    _, report = apply_edits(build_pdf(), [], [ann])
    assert report[0]["index"] == 41
    assert report[0]["annotation"] == "highlight"


def test_markups_without_an_index_still_get_unique_report_rows():
    # A caller that forgets indices must not have rows overwrite each other.
    raw = build_pdf("Replace this line")
    spans = pdf_ops.collect_spans(pymupdf.open(stream=raw, filetype="pdf")[0], 0)
    edit = Edit(page=0, rect=spans[0].bbox, text="Replaced", index=0)
    anns = [Annotation.from_dict({"kind": "highlight", "page": 0,
                                  "rect": [10.0, 10.0, 60.0, 40.0]}) for _ in range(3)]
    _, report = apply_edits(raw, [edit], anns)
    assert [row["index"] for row in report] == [0, 1, 2, 3]
