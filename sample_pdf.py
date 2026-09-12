"""Generates a demo PDF so the editor can be tried without hunting for a file.

The sample deliberately includes the awkward cases: text sitting on a coloured
panel (a white-box redaction would show up immediately), rules and table borders
(a careless redaction would eat them), several typefaces, and a rotated page.
"""

from __future__ import annotations

import pymupdf

INK = (0.11, 0.12, 0.15)
MUTED = (0.42, 0.45, 0.5)
ACCENT = (0.09, 0.32, 0.62)
PANEL = (0.93, 0.95, 0.99)
GRID = (0.78, 0.81, 0.86)
HEADER_ROW = (0.16, 0.22, 0.36)

BODY_FONT = "helv"
BODY_SIZE = 11


def _heading_font() -> tuple[str | None, str]:
    """An installed bold serif face for the headings, when there is one.

    The sample deliberately mixes an embedded face (headings) with the standard
    14 (body), so editing either kind shows what the editor did with the font.
    """
    try:
        import font_matching
        for font in font_matching.system_fonts.scan():
            if font.kind == "serif" and font.bold and not font.italic:
                return font.path, "HEAD"
    except Exception:
        pass
    return None, "tibo"


def _paragraph(page: pymupdf.Page, x: float, y: float, lines: list[str],
               size: float = BODY_SIZE, font: str = BODY_FONT,
               color: tuple[float, float, float] = INK, leading: float = 16.0) -> float:
    for index, line in enumerate(lines):
        page.insert_text((x, y + index * leading), line, fontname=font, fontsize=size, color=color)
    return y + (len(lines) - 1) * leading


def build_sample() -> bytes:
    doc = pymupdf.open()
    heading_file, heading_font = _heading_font()
    heading_kwargs: dict = {"fontname": heading_font}
    if heading_file:
        heading_kwargs["fontfile"] = heading_file

    # ---------------------------------------------------------------- page 1
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "Quarterly Operations Report", fontsize=21, color=INK,
                     **({**heading_kwargs, "fontname": heading_file and "HEAD" or "hebo"}))
    page.insert_text((60, 102), "Prepared by the Operations Team  ·  Q3", fontname="helv",
                     fontsize=10.5, color=MUTED)
    page.draw_line(pymupdf.Point(60, 116), pymupdf.Point(535, 116), color=GRID, width=1)

    page.insert_text((60, 158), "1.  Summary", fontsize=14, color=ACCENT,
                     **heading_kwargs)
    _paragraph(page, 60, 184, [
        "Throughput improved for the third consecutive quarter, driven mainly by the",
        "scheduling changes introduced in July. Two of the four legacy pipelines were",
        "retired and the remaining pair now run at roughly eighty percent utilisation.",
    ])

    # A coloured panel with text on it: the classic trap for a naive redaction,
    # which would paste a white box over the panel and destroy the design.
    panel = pymupdf.Rect(60, 268, 535, 340)
    page.draw_rect(panel, color=None, fill=PANEL)
    page.draw_line(pymupdf.Point(60, 268), pymupdf.Point(60, 340), color=ACCENT, width=3)
    page.insert_text((78, 296), "Headline metric", fontname="hebo", fontsize=11.5, color=ACCENT)
    page.insert_text((78, 316), "Median job latency fell from 4.8 s to 1.9 s during the quarter.",
                     fontname="helv", fontsize=11, color=INK)
    page.insert_text((78, 330), "No customer-visible incidents were recorded in the period.",
                     fontname="helv", fontsize=11, color=INK)

    page.insert_text((60, 382), "2.  Notes", fontsize=14, color=ACCENT,
                     **heading_kwargs)
    _paragraph(page, 60, 408, [
        "•  The three remaining manual steps are documented in the appendix.",
        "•  Capacity headroom is sufficient for the next two quarters.",
        "•  Archive storage is approaching the threshold set in the platform plan.",
    ], leading=18)

    page.insert_text((60, 500), "3.  Costs", fontsize=14, color=ACCENT,
                     **heading_kwargs)
    _paragraph(page, 60, 526, [
        "Unit cost per thousand jobs fell to 0.42 EUR. The reduction is almost entirely",
        "attributable to the smaller machine pool rather than to a change in pricing.",
    ], leading=16, font="tiro")

    page.insert_text((60, 812), "Confidential  ·  page 1 of 3", fontname="cour", fontsize=8.5, color=MUTED)

    # ---------------------------------------------------------------- page 2
    table = doc.new_page(width=595, height=842)
    table.insert_text((60, 80), "4.  Line items", fontsize=14, color=ACCENT,
                      **heading_kwargs)

    columns = (60.0, 250.0, 370.0, 470.0)
    headers = ("Component", "Volume", "Unit cost", "Total")
    rows = [
        ("Ingest workers", "1,284,000", "0.11", "141,240"),
        ("Transform stage", "1,284,000", "0.14", "179,760"),
        ("Index writers", "962,000", "0.09", "86,580"),
        ("Cold archive", "418,000", "0.05", "20,900"),
        ("Monitoring", "—", "—", "12,400"),
    ]
    y = 118.0
    row_height = 26.0
    page_header = pymupdf.Rect(columns[0], y - 18, columns[-1] + 70, y + 8)
    table.draw_rect(page_header, color=None, fill=HEADER_ROW)
    for x, label in zip(columns, headers):
        table.insert_text((x + 8, y), label, fontname="hebo", fontsize=11, color=(1, 1, 1))

    y += row_height
    for index, row in enumerate(rows):
        if index % 2 == 0:
            table.draw_rect(pymupdf.Rect(columns[0], y - 17, columns[-1] + 70, y + 7),
                            color=None, fill=(0.97, 0.975, 0.985))
        for x, value in zip(columns, row):
            table.insert_text((x + 8, y), value, fontname="helv", fontsize=10.5, color=INK)
        table.draw_line(pymupdf.Point(columns[0], y + 8), pymupdf.Point(columns[-1] + 70, y + 8),
                        color=GRID, width=0.6)
        y += row_height

    table.draw_line(pymupdf.Point(columns[0], y - 18), pymupdf.Point(columns[-1] + 70, y - 18),
                    color=GRID, width=1)
    table.insert_text((columns[0] + 8, y + 4), "Total", fontname="hebo", fontsize=11, color=INK)
    table.insert_text((columns[-1] + 8, y + 4), "440,880", fontname="hebo", fontsize=11, color=INK)
    table.insert_text((60, 812), "Confidential  ·  page 2 of 3", fontname="cour", fontsize=8.5, color=MUTED)

    # ---------------------------------------------------------------- page 3
    # A rotated page: proves the coordinate contract holds when /Rotate is set.
    appendix = doc.new_page(width=595, height=842)
    appendix.insert_text((60, 80), "Appendix", fontsize=14, color=ACCENT,
                         **heading_kwargs)
    _paragraph(appendix, 60, 110, [
        "This page carries a rotation flag of ninety degrees, so it is displayed in",
        "landscape. Editing text here exercises the coordinate conversion end to end.",
    ])
    appendix.insert_text((60, 200), "Rotated page — try editing this line.",
                         fontname="hebo", fontsize=12, color=ACCENT)

    # Subsetting keeps the demo small; the headings' font keeps its character map
    # because subset_fonts() rewrites only as much as it must.
    appendix.set_rotation(90)

    raw = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return raw
