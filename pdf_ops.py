"""The editing engine: find the text under a rectangle, delete it, write the replacement.

Coordinate contract (the most important thing in this file)
----------------------------------------------------------
Everything here uses **PyMuPDF page space**:

* origin top-left, y grows downward
* units are PDF points, never scaled by zoom
* dimensions are the *unrotated* cropbox (``page.mediabox``), **not** ``page.rect``

That last point is subtle and was verified experimentally against rendered
pixels: on a page with ``/Rotate 90``, ``page.rect`` reports the rotated display
size while ``get_text()`` and ``insert_text()`` both keep working in unrotated
page space. Sticking to unrotated space means a single coordinate system holds
for every rotation, and the server needs no derotation math at all. The browser
converts its display-space click into this space before sending an edit.

How an edit is applied
----------------------
1. Find the existing text under the rect (used for the report, and to inherit
   the original font, size, colour and baseline).
2. Mark the rect as a redaction annotation and remove the original glyphs.
3. Draw the new text on the original baseline.

See ``REDACT_KWARGS`` for why step 2 does not damage the page.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import Any, Sequence

import pymupdf

import font_matching

__all__ = [
    "REDACT_KWARGS",
    "ANNOTATION_KINDS",
    "ANNOTATION_LABELS",
    "Span",
    "Edit",
    "Annotation",
    "InvalidDocument",
    "open_document",
    "page_sizes",
    "collect_spans",
    "find_text",
    "apply_edits",
]

# --------------------------------------------------------------------------
# Glyph removal. The defaults of page.apply_redactions() would damage the page:
#   text=PDF_REDACT_TEXT_REMOVE is 0, which happens to be the default, but
#   graphics defaults to REMOVE_IF_COVERED and images to IMAGE_PIXELS, so any
#   artwork or photo under the edit box gets destroyed/pixelated. Be explicit.
#   Removing text without painting anything is also what keeps the page
#   background intact, so the exported result matches the on-screen preview.
# --------------------------------------------------------------------------
REDACT_KWARGS: dict[str, int] = {
    "images": pymupdf.PDF_REDACT_IMAGE_NONE,
    "graphics": pymupdf.PDF_REDACT_LINE_ART_NONE,
    "text": pymupdf.PDF_REDACT_TEXT_REMOVE,
}

# The 12 base-14 fonts, keyed by (family, bold, italic).
BASE14: dict[tuple[str, bool, bool], str] = {
    ("sans", False, False): "helv",
    ("sans", True, False): "hebo",
    ("sans", False, True): "heit",
    ("sans", True, True): "hebi",
    ("serif", False, False): "tiro",
    ("serif", True, False): "tibo",
    ("serif", False, True): "tiit",
    ("serif", True, True): "tibi",
    ("mono", False, False): "cour",
    ("mono", True, False): "cobo",
    ("mono", False, True): "coit",
    ("mono", True, True): "cobi",
}

BASE14_LABEL = {
    "helv": "Helvetica", "hebo": "Helvetica-Bold", "heit": "Helvetica-Oblique",
    "hebi": "Helvetica-BoldOblique", "tiro": "Times-Roman", "tibo": "Times-Bold",
    "tiit": "Times-Italic", "tibi": "Times-BoldItalic", "cour": "Courier",
    "cobo": "Courier-Bold", "coit": "Courier-Oblique", "cobi": "Courier-BoldOblique",
}

# Span flags (MuPDF): 1 superscript, 2 italic, 4 serifed, 8 monospaced, 16 bold.
FLAG_ITALIC = 1 << 1
FLAG_SERIF = 1 << 2
FLAG_MONO = 1 << 3
FLAG_BOLD = 1 << 4

# Font-name hints, checked before the flags (names are more reliable in the wild).
_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("courier", "mono"), ("consol", "mono"), ("mono", "mono"), ("menlo", "mono"),
    ("times", "serif"), ("georgia", "serif"), ("garamond", "serif"),
    ("cambria", "serif"), ("book", "serif"), ("palatino", "serif"),
    ("minion", "serif"), ("serif", "serif"),
    ("arial", "sans"), ("helvetic", "sans"), ("calibri", "sans"), ("segoe", "sans"),
    ("verdana", "sans"), ("tahoma", "sans"), ("roboto", "sans"), ("lato", "sans"),
    ("futura", "sans"), ("gill", "sans"), ("sans", "sans"),
)
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "-bd", "medi")
_ITALIC_HINTS = ("italic", "oblique", "slanted", "-it", "itm")

# A rect must overlap a span by at least this fraction of the span to count as
# "this text is under the cursor".
MIN_COVERAGE = 0.25
# Tolerance used to still find text when the client rect is a hair off.
SNAP_TOLERANCE = 2.0
# How far a rect may sit from any text and still borrow that text's styling.
# Beyond this we hand back no styling at all rather than reach across a whole
# line or table row, which would misreport what lives under the cursor.
NEAR_DISTANCE = 24.0


class InvalidDocument(ValueError):
    """Raised when the uploaded bytes are not a usable PDF."""


def open_document(raw: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:  # pragma: no cover - depends on malformed input
        raise InvalidDocument(f"could not read PDF: {exc}") from exc
    if doc.needs_pass:
        raise InvalidDocument("this PDF is password protected")
    if doc.page_count == 0:
        raise InvalidDocument("this PDF has no pages")
    return doc


def page_sizes(doc: pymupdf.Document) -> list[dict[str, Any]]:
    """Per-page geometry. `width`/`height` are unrotated; `display_*` are what the viewer shows."""
    out: list[dict[str, Any]] = []
    for index, page in enumerate(doc):
        box, rect = page.mediabox, page.rect
        out.append({
            "index": index,
            "width": round(box.width, 2),
            "height": round(box.height, 2),
            "rotation": page.rotation,
            "display_width": round(rect.width, 2),
            "display_height": round(rect.height, 2),
        })
    return out


def _rgb_from_int(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


def rgb_to_hex(color: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in color)


def _clean(text: str) -> str:
    """PDF text extraction sprinkles non-breaking and soft hyphens around."""
    return (text or "").replace("\xa0", " ").replace("\u00ad", "").replace("\u200b", "")


def classify_font(name: str, flags: int) -> tuple[str, bool, bool]:
    low = (name or "").lower()
    bold = bool(flags & FLAG_BOLD) or any(h in low for h in _BOLD_HINTS)
    italic = bool(flags & FLAG_ITALIC) or any(h in low for h in _ITALIC_HINTS)
    family = next((fam for hint, fam in _FAMILY_HINTS if hint in low), None)
    if family is None:
        family = "mono" if flags & FLAG_MONO else ("serif" if flags & FLAG_SERIF else "sans")
    return family, bold, italic


@dataclass(frozen=True)
class Span:
    """One run of text as the PDF engine sees it, in unrotated page space."""

    page: int
    text: str
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]  # baseline start point
    size: float
    font: str
    flags: int
    color: tuple[float, float, float]
    family: str
    bold: bool
    italic: bool

    @property
    def base14(self) -> str:
        return BASE14[(self.family, self.bold, self.italic)]

    @property
    def font_label(self) -> str:
        return BASE14_LABEL[self.base14]

    @property
    def rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.bbox)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "origin": [round(v, 2) for v in self.origin],
            "size": round(self.size, 2),
            "font": self.font,
            "family": self.family,
            "bold": self.bold,
            "italic": self.italic,
            "color": [round(c, 4) for c in self.color],
        }


def collect_spans(page: pymupdf.Page, page_index: int) -> list[Span]:
    """Every text run on a page, in reading order."""
    found: list[Span] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", ()):
        if block.get("type", 0) != 0:  # skip images
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                text = _clean(span.get("text", ""))
                if not text.strip():
                    continue
                size = float(span.get("size") or 0.0)
                if size <= 0:
                    continue
                family, bold, italic = classify_font(span.get("font", ""), int(span.get("flags") or 0))
                found.append(Span(
                    page=page_index,
                    text=text,
                    bbox=tuple(float(v) for v in span["bbox"]),  # type: ignore[arg-type]
                    origin=tuple(float(v) for v in span["origin"]),  # type: ignore[arg-type]
                    size=size,
                    font=span.get("font", "") or "",
                    flags=int(span.get("flags") or 0),
                    color=_rgb_from_int(int(span.get("color") or 0)),
                    family=family,
                    bold=bold,
                    italic=italic,
                ))
    return found


def _intersection(a: pymupdf.Rect, b: pymupdf.Rect) -> pymupdf.Rect | None:
    inter = a & b
    if inter.is_empty or inter.width <= 0 or inter.height <= 0:
        return None
    return inter


def spans_under(spans: Sequence[Span], rect: pymupdf.Rect) -> list[tuple[float, Span]]:
    """Spans covered by `rect`, best covered first.

    Scored by how much *of the span* the rect covers, so a click on one word of a
    multi-word run still ranks that run first rather than a stray comma that
    happens to sit inside the rect.
    """
    if rect.is_empty:
        return []
    scored: list[tuple[float, Span]] = []
    for span in spans:
        span_rect = span.rect
        if span_rect.is_empty:
            continue
        inter = _intersection(rect, span_rect)
        if inter is None:
            continue
        coverage = inter.get_area() / max(span_rect.get_area(), 0.01)
        if coverage >= MIN_COVERAGE:
            scored.append((coverage, span))
    scored.sort(key=lambda item: (-item[0], item[1].bbox[1], item[1].bbox[0]))
    return scored


def _span_centre(span: Span) -> tuple[float, float]:
    return ((span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2)


def find_text(spans: Sequence[Span], rect: pymupdf.Rect) -> dict[str, Any]:
    """What text lives under `rect`, plus the style a replacement should inherit.

    `found` is deliberately strict: it is only true when text genuinely
    overlaps the box. A nearby run may still supply style defaults (so adding
    text next to a paragraph picks up its typeface), but that never counts as
    something to remove -- claiming otherwise would let an export report
    "replaced" for a spot that had nothing in it.
    """
    hits = spans_under(spans, rect)
    approx = False
    if not hits:
        grown = pymupdf.Rect(rect.x0 - SNAP_TOLERANCE, rect.y0 - SNAP_TOLERANCE,
                              rect.x1 + SNAP_TOLERANCE, rect.y1 + SNAP_TOLERANCE)
        hits = spans_under(spans, grown)
        approx = bool(hits)

    style_span: Span | None = hits[0][1] if hits else None
    defaults_from = "match" if style_span else "none"
    if style_span is None:
        centre = ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        nearby = sorted(spans, key=lambda s: math.dist(centre, _span_centre(s)))
        if nearby and math.dist(centre, _span_centre(nearby[0])) <= NEAR_DISTANCE:
            style_span = nearby[0]
            defaults_from = "nearest"

    # Join every span that is essentially inside the box, in reading order.
    picked = [span for coverage, span in hits if coverage >= 0.5]
    if not picked and hits:
        picked = [hits[0][1]]
    picked.sort(key=lambda s: (round(s.bbox[1], 1), s.bbox[0]))
    original = " ".join(s.text.strip() for s in picked).strip()

    defaults: dict[str, Any] = {
        "font": style_span.base14 if style_span else "helv",
        # The base-14 face this run would fall back to; the export may still reuse
        # the document's own embedded font instead, which the report states per edit.
        "font_label": style_span.font_label if style_span else "Helvetica",
        "font_name": style_span.font if style_span else None,
        "bold": style_span.bold if style_span else None,
        "italic": style_span.italic if style_span else None,
        # A CSS stack the browser can preview with, ending in a generic family.
        "css_family": (font_matching.css_family_stack(style_span.font, style_span.family)
                       if style_span else "sans-serif"),
        "size": round(style_span.size, 2) if style_span else None,
        "color": [round(c, 4) for c in (style_span.color if style_span else (0.0, 0.0, 0.0))],
        "from": defaults_from,
    }
    if style_span is not None:
        defaults["baseline"] = [round(style_span.origin[0], 2), round(style_span.origin[1], 2)]
    return {
        "found": bool(picked),
        "approx": approx,
        "text": original,
        "matches": [s.as_dict() for _, s in hits[:8]],
        "defaults": defaults,
    }


# PDF names of the standard 14, so a document that uses one can be recognised
# and reused exactly rather than approximated.
BASE14_BY_NAME: dict[str, str] = {
    "helvetica": "helv", "helvetica-bold": "hebo",
    "helvetica-oblique": "heit", "helvetica-boldoblique": "hebi",
    "times-roman": "tiro", "times-bold": "tibo",
    "times-italic": "tiit", "times-bolditalic": "tibi",
    "courier": "cour", "courier-bold": "cobo",
    "courier-oblique": "coit", "courier-boldoblique": "cobi",
    "symbol": "symb", "zapfdingbats": "zadb",
}


# A font bigger than this is not worth dragging into the document; the
# fallback faces below will look close enough.
MAX_REUSE_BYTES = 8 * 1024 * 1024


def base14_for_name(name: str | None) -> str | None:
    """``"Helvetica-Bold"`` -> ``"hebo"``; None when it is not a standard font."""
    key = (name or "").strip().lower().replace(" ", "")
    return BASE14_BY_NAME.get(key)


@dataclass(frozen=True)
class FontChoice:
    """Which face a replacement should be drawn in, and how faithful that is.

    The font is *described* here but only registered on the page at draw time:
    ``insert_font`` has to happen after ``apply_redactions``, because redaction
    rewrites the page's content stream and resources and drops a not-yet-used
    font. Registering early silently degrades the replacement to MuPDF's default
    face -- the text still reads correctly, in entirely the wrong typeface.
    """

    label: str                        # what to show the user
    source: str                       # exact | matched | approximate | requested | none
    note: str
    base14: str | None = None         # a built-in face, usable directly
    embed: bytes | None = None        # an embedded font to register at draw time
    embed_path: str | None = None     # ... or an installed font file
    fallback14: str | None = None     # built-in face to use if registration fails


class _FontResolver:
    """Picks the closest thing to the document's own font for each replacement.

    In order of preference:

    1. **exact** -- the document's own font: either one of the standard 14 that
       the original was drawn with, or its embedded font re-inserted as-is.
    2. **matched** -- the closest face installed on this machine, chosen by
       family name, aliases, and bold/italic/serif class.
    3. **approximate** -- a built-in base-14 face.

    Every candidate has to pass a render test in a scratch document before it is
    used. That is not paranoia: a font whose character map was stripped by
    subsetting accepts the call and silently emits null bytes instead of glyphs.
    """

    def __init__(self, doc: pymupdf.Document) -> None:
        self.doc = doc
        self._page_maps: dict[int, dict[str, list[tuple]]] = {}
        self._buffers: dict[int, bytes | None] = {}
        self._registered: dict[tuple[int, str], str] = {}
        self._preflight: dict[tuple, bool] = {}
        self._parsed: dict[str, pymupdf.Font] = {}
        self._counter = 0
        self.embedded_any = False

    # -- page font inventory ----------------------------------------------

    def _page_map(self, page: pymupdf.Page, index: int) -> dict[str, list[tuple]]:
        found = self._page_maps.get(index)
        if found is None:
            found = {}
            for record in page.get_fonts(full=True):
                xref, ext, kind, basefont, refname = (list(record) + [None] * 5)[:5]
                entry = (xref, (ext or "").lower(), kind or "", basefont or "", refname or "")
                for key in filter(None, (font_matching.name_key(basefont),
                                         font_matching.family_key(basefont))):
                    found.setdefault(key, []).append(entry)
            self._page_maps[index] = found
        return found

    def _entry_for(self, page: pymupdf.Page, index: int,
                   name: str | None, bold: bool | None, italic: bool | None) -> tuple | None:
        fonts = self._page_map(page, index)
        if not fonts:
            return None
        if not name:
            return next(iter(fonts.values()))[0]
        for key in (font_matching.name_key(name), font_matching.family_key(name)):
            candidates = fonts.get(key) if key else None
            if not candidates:
                continue
            if len(candidates) == 1:
                return candidates[0]
            # Same family, several faces on the page: take the one matching the run.
            for entry in candidates:
                if font_matching.style_of(entry[3]) == (bool(bold), bool(italic)):
                    return entry
            return candidates[0]
        return next(iter(fonts.values()))[0] if len(fonts) == 1 else None

    def _buffer(self, xref: int) -> bytes | None:
        if xref not in self._buffers:
            try:
                _name, _ext, _kind, buf = self.doc.extract_font(xref)
                self._buffers[xref] = bytes(buf) if buf else None
            except Exception:
                self._buffers[xref] = None
        return self._buffers[xref]

    # -- does this font really work? ---------------------------------------

    def _can_render(self, source: bytes | str, text: str) -> bool:
        """Can this font draw `text` -- and is it really *this* font that draws it?

        Both halves matter. A font whose character map was stripped by subsetting
        accepts the call and emits null bytes, and a font MuPDF cannot embed at
        all (a ``.ttc`` collection, say) is silently swapped for a built-in face:
        the text still spells correctly, in the wrong typeface. So the text is
        checked *and* the resulting run's family is compared with the source's.
        """
        key = (hash(source) if isinstance(source, bytes) else source, text)
        cached = self._preflight.get(key)
        if cached is not None:
            return cached
        ok = False
        try:
            if isinstance(source, bytes):
                parsed = pymupdf.Font(fontbuffer=source)
            else:
                parsed = pymupdf.Font(fontfile=source)
            expected = font_matching.family_key(getattr(parsed, "name", ""))
            scratch = pymupdf.open()
            try:
                scratch_page = scratch.new_page(width=400, height=200)
                if isinstance(source, bytes):
                    scratch_page.insert_font(fontname="T", fontbuffer=source)
                else:
                    scratch_page.insert_font(fontname="T", fontfile=source)
                scratch_page.insert_text((20, 100), text, fontname="T", fontsize=12)
                drawn = {font_matching.family_key(span["font"])
                         for block in scratch_page.get_text("dict")["blocks"]
                         for line in block.get("lines", ())
                         for span in line.get("spans", ())}
                ok = text.strip() in _clean(scratch_page.get_text())
                if expected:
                    ok = ok and expected in drawn
            finally:
                scratch.close()
        except Exception:
            ok = False
        self._preflight[key] = ok
        return ok

    def register(self, page: pymupdf.Page, index: int, choice: FontChoice) -> str:
        """Make a choice usable on this page. Must run AFTER apply_redactions()."""
        if choice.base14:
            return choice.base14
        key = (index, f"buf{hash(choice.embed)}" if choice.embed else f"path{choice.embed_path}")
        existing = self._registered.get(key)
        if existing:
            return existing
        self._counter += 1
        name = f"PDFEDF{self._counter}"
        try:
            if choice.embed:
                page.insert_font(fontname=name, fontbuffer=choice.embed)
            else:
                page.insert_font(fontname=name, fontfile=choice.embed_path)
        except Exception:
            return choice.fallback14 or "helv"
        self._registered[key] = name
        self.embedded_any = True
        return name

    def measure(self, choice: FontChoice, text: str, fontsize: float) -> float | None:
        """Width of `text` in the chosen face, so autofit uses real metrics."""
        try:
            if choice.embed or choice.embed_path:
                key = f"buf{hash(choice.embed)}" if choice.embed else f"path{choice.embed_path}"
                parsed = self._parsed.get(key)
                if parsed is None:
                    parsed = (pymupdf.Font(fontbuffer=choice.embed) if choice.embed
                              else pymupdf.Font(fontfile=choice.embed_path))
                    self._parsed[key] = parsed
                return float(parsed.text_length(text, fontsize))
            if choice.base14:
                return float(pymupdf.get_text_length(text, fontname=choice.base14, fontsize=fontsize))
        except Exception:
            return None
        return None

    # -- the decision ------------------------------------------------------

    def resolve(self, page: pymupdf.Page, index: int, text: str, *,
                name: str | None = None, bold: bool | None = None,
                italic: bool | None = None) -> FontChoice:
        """Choose the face for `text`, matching a run named `name` where given."""
        entry = self._entry_for(page, index, name, bold, italic)
        original = name or (entry[3] if entry else "") or ""
        span_family, span_bold, span_italic = classify_font(original, 0)
        # A run's own flags beat its name: a face can be bold without saying so
        # (an embedded copy named "Helvetica", say), and trusting the name there
        # would quietly pick the regular cut and lose the weight.
        by_name = base14_for_name(original) if bold is None and italic is None else None
        standard = by_name or BASE14[
            (span_family, bool(bold), bool(italic))]
        standard_label = BASE14_LABEL.get(standard, standard)

        # 1. The document's own font.
        if entry is None or entry[1] in ("n/a", ""):
            return FontChoice(standard_label, "exact",
                              f"the document is set in {standard_label}, one of the standard 14",
                              base14=standard)
        if base14_for_name(original):
            # An embedded copy of a standard face renders the same as the built-in.
            return FontChoice(standard_label, "exact",
                              f"the document is set in {standard_label}", base14=standard)
        buffer = self._buffer(entry[0])
        if buffer and len(buffer) <= MAX_REUSE_BYTES and self._can_render(buffer, text):
            return FontChoice(original or standard_label, "exact",
                              "kept the document's own embedded font",
                              embed=buffer, fallback14=standard)

        # 2. The closest face installed here.
        match = font_matching.system_fonts.best_match(
            original, text, bold=bold, italic=italic)
        if match is not None and self._can_render(match.path, text):
            return FontChoice(match.label(), "matched",
                              f"closest installed match for \"{original}\"",
                              embed_path=match.path, fallback14=standard)

        # 3. Something built in, at least.
        return FontChoice(standard_label, "approximate",
                          f"\"{original or 'the original font'}\" could not be reused; "
                          f"approximated with {standard_label}", base14=standard)


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------

@dataclass
class Edit:
    """One requested change. `rect` is in unrotated page space (see module docstring)."""

    page: int
    rect: tuple[float, float, float, float]
    text: str = ""
    mode: str = "replace"  # replace | delete
    font: str | None = None
    size: float | None = None
    color: tuple[float, float, float] | None = None
    align: int = 0
    autofit: bool = False
    pad: float = 0.0
    index: int = -1
    problem: str | None = None

    @classmethod
    def from_dict(cls, data: Any, index: int = -1) -> "Edit":
        if not isinstance(data, dict):
            return cls(page=0, rect=(0, 0, 0, 0), index=index, problem="edit must be an object")
        raw_rect = data.get("rect")
        problem = None
        try:
            rect = tuple(float(v) for v in raw_rect)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            rect = (0.0, 0.0, 0.0, 0.0)
            problem = "rect must be four numbers"
        if problem is None and (len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]):
            problem = "rect must be a non-empty box"
        try:
            page = int(data.get("page", -1))
        except (TypeError, ValueError):
            page, problem = -1, problem or "page must be an integer"
        mode = str(data.get("mode") or "replace").lower()
        if mode not in ("replace", "delete"):
            mode = "replace"
        font = data.get("font")
        font = str(font) if font and str(font) != "auto" else None
        if font is not None and font not in BASE14_LABEL:
            font = None
        try:
            size = float(data["size"]) if data.get("size") not in (None, "", "auto") else None
        except (TypeError, ValueError):
            size = None
        if size is not None and not (2 <= size <= 400):
            size = None
        color = data.get("color")
        try:
            color = tuple(float(c) for c in color) if color else None  # type: ignore[union-attr]
        except (TypeError, ValueError):
            color = None
        if color is not None and len(color) != 3:
            color = None
        try:
            pad = max(0.0, min(float(data.get("pad") or 0.0), 40.0))
        except (TypeError, ValueError):
            pad = 0.0
        return cls(
            page=page,
            rect=rect,  # type: ignore[arg-type]
            text=str(data.get("text") or ""),
            mode=mode,
            font=font,
            size=size,
            color=color,  # type: ignore[arg-type]
            align=int(data.get("align") or 0) if str(data.get("align") or 0).lstrip("-").isdigit() else 0,
            autofit=bool(data.get("autofit")),
            pad=pad,
            index=index,
            problem=problem,
        )


def _fit_size(text: str, choice: "FontChoice", fonts: "_FontResolver", size: float,
              max_width: float, floor_ratio: float = 0.45) -> float:
    """Shrink `size` (never below floor_ratio of it) so `text` fits `max_width`."""
    if max_width <= 1 or size <= 0:
        return size
    width = fonts.measure(choice, text, size)
    if width is None:
        try:
            width = pymupdf.get_text_length(text, fontname=choice.base14 or "helv", fontsize=size)
        except Exception:
            return size
    if width <= max_width or width <= 0:
        return size
    return max(size * (max_width / width), size * floor_ratio)


def _insert_plain(page: pymupdf.Page, point: tuple[float, float], text: str,
                  fontname: str, fontsize: float, color: tuple[float, float, float]) -> None:
    page.insert_text(point, text, fontname=fontname, fontsize=fontsize, color=color, render_mode=0)


def _insert_rich(page: pymupdf.Page, rect: pymupdf.Rect, text: str,
                 fontsize: float, color: tuple[float, float, float]) -> tuple[float, float]:
    """Fallback for text the base-14 fonts cannot encode (CJK, emoji, exotic scripts).

    ``insert_htmlbox`` reaches for a system font and auto-shrinks to fit.
    Returns (spare_height, scale).
    """
    import html as _html

    rgb = "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in color)
    css = f"* {{ font-family: sans-serif; font-size: {max(fontsize, 4):.2f}px; color: {rgb}; }}"
    box = pymupdf.Rect(rect.x0, rect.y0, max(rect.x1, rect.x0 + 4), max(rect.y1, rect.y0 + fontsize * 1.6))
    spare, scale = page.insert_htmlbox(box, _html.escape(text).replace("\n", "<br>"), css=css)
    return float(spare), float(scale)


def _latin1_safe(text: str) -> bool:
    """Base-14 with WinAnsi covers ASCII, Latin-1 and a few common punctuation marks."""
    winansi_extras = "\u2018\u2019\u201c\u201d\u2013\u2014\u2022\u20ac\u2026\u2122"
    return all(ord(ch) < 0x100 or ch in winansi_extras for ch in text)


def _baseline_for(edit: Edit, span: Span | None, size: float) -> tuple[float, float]:
    """Where the replacement text sits.

    Inheriting the original baseline is what makes a replacement land exactly
    where the old text sat, instead of floating inside the box. The x always
    comes from the edit rect so a partial replacement stays put.
    """
    x0, _y0, _x1, y1 = edit.rect
    if span is not None:
        return (x0, span.origin[1])
    return (x0, min(y1 - size * 0.22, y1))


# --------------------------------------------------------------------------
# Markups
#
# Everything the toolbar can draw on top of a page lives here: freehand ink,
# arrows, shapes, highlights, new text boxes and images. They share the text
# editor's coordinate space exactly -- PDF points, unrotated page space, origin
# top-left -- so one conversion in the browser feeds both, and a markup lands
# where the user drew it at any page rotation.
#
# They are drawn *after* the redaction pass for the page: apply_redactions()
# rewrites the content stream and would eat anything painted before it. The
# ordering also puts a markup on top of the replacement text, which is what a
# reader expects from a pen stroke.
# --------------------------------------------------------------------------

ANNOTATION_KINDS: tuple[str, ...] = (
    "text", "image", "ink", "line", "arrow", "rect", "ellipse", "highlight",
)
ANNOTATION_LABELS: dict[str, str] = {
    "text": "Text box", "image": "Image", "ink": "Freehand", "line": "Line",
    "arrow": "Arrow", "rect": "Rectangle", "ellipse": "Ellipse",
    "highlight": "Highlight",
}
# Defined by a box ...
BOX_ANNOTATIONS = frozenset(("text", "image", "rect", "ellipse", "highlight"))
# ... or by a list of points.
POINT_ANNOTATIONS = frozenset(("ink", "line", "arrow"))
_FILLABLE = frozenset(("rect", "ellipse"))

MAX_ANNOTATION_IMAGE_BYTES = 12 * 1024 * 1024
DEFAULT_HIGHLIGHT_OPACITY = 0.35
ARROW_HEAD_ANGLE = math.radians(24)


def _as_floats(value: Any, count: int) -> tuple[float, ...] | None:
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return out if len(out) == count else None


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default


@dataclass
class Annotation:
    """One markup drawn on top of a page, in the same space as an :class:`Edit`."""

    kind: str
    page: int
    rect: tuple[float, float, float, float] | None = None
    points: tuple[tuple[float, float], ...] = ()
    text: str = ""
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    width: float = 2.0
    opacity: float = 1.0
    fill_opacity: float = 0.0
    size: float = 12.0
    font: str | None = None
    align: int = 0
    bold: bool = False
    italic: bool = False
    image: bytes | None = None
    index: int = -1
    problem: str | None = None

    @property
    def label(self) -> str:
        return ANNOTATION_LABELS.get(self.kind, "Markup")

    @property
    def box(self) -> pymupdf.Rect:
        """The markup's extent, from its rect or the bounds of its points."""
        if self.rect:
            return pymupdf.Rect(self.rect)
        if self.points:
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
        return pymupdf.Rect()

    def describe(self) -> str:
        """A one-line geometry summary for the export report."""
        box = self.box
        if self.kind in POINT_ANNOTATIONS:
            if self.kind == "ink":
                size = f"{len(self.points)} points · {self.width:g}pt"
            else:
                length = math.dist(self.points[0], self.points[-1]) if len(self.points) > 1 else 0.0
                size = f"{length:.0f}pt long · {self.width:g}pt"
        else:
            size = f"{box.width:.0f}×{box.height:.0f}pt at {box.x0:.0f},{box.y0:.0f}"
        if self.kind in _FILLABLE and self.fill_opacity > 0:
            size += " · filled"
        if self.kind == "highlight":
            size += f" · {round((self.fill_opacity or DEFAULT_HIGHLIGHT_OPACITY) * 100)}% opacity"
        elif self.kind == "image" and self.image:
            size += f" · {max(1, round(len(self.image) / 1024))} KB"
        return size

    @classmethod
    def from_dict(cls, data: Any, index: int = -1) -> "Annotation":
        """Validate a client payload. A rejected markup keeps its `problem`."""
        if not isinstance(data, dict):
            return cls(kind="unknown", page=0, index=index,
                       problem="annotation must be an object")
        raw_kind = str(data.get("kind") or "").strip().lower()
        problem: str | None = None
        if raw_kind not in ANNOTATION_KINDS:
            problem = f"unknown annotation kind {raw_kind!r}"
        kind = raw_kind if raw_kind in ANNOTATION_KINDS else "unknown"

        try:
            page = int(data.get("page", -1))
        except (TypeError, ValueError):
            page, problem = -1, problem or "page must be an integer"

        rect: tuple[float, float, float, float] | None = None
        raw_rect = data.get("rect")
        if raw_rect:
            parsed = _as_floats(raw_rect, 4)
            if parsed is None or parsed[2] <= parsed[0] or parsed[3] <= parsed[1]:
                problem = problem or "rect must be a non-empty box"
            else:
                rect = (parsed[0], parsed[1], parsed[2], parsed[3])
        elif kind in BOX_ANNOTATIONS:
            problem = problem or f"a {kind} markup needs a rect"

        points: list[tuple[float, float]] = []
        raw_points = data.get("points") or []
        if isinstance(raw_points, (list, tuple)):
            for point in raw_points:
                pair = _as_floats(point, 2)
                if pair is not None:
                    points.append((pair[0], pair[1]))
        if kind in POINT_ANNOTATIONS and len(points) < 2:
            problem = problem or f"a {kind} markup needs at least two points"

        color = _as_floats(data.get("color") or (0.0, 0.0, 0.0), 3) or (0.0, 0.0, 0.0)
        color = tuple(max(0.0, min(1.0, c)) for c in color)  # type: ignore[assignment]

        image: bytes | None = None
        raw_image = data.get("image")
        if raw_image:
            blob = raw_image.get("data") if isinstance(raw_image, dict) else raw_image
            try:
                image = base64.b64decode(str(blob))
            except Exception:
                image, problem = None, problem or "image data could not be decoded"
            if image is not None and len(image) > MAX_ANNOTATION_IMAGE_BYTES:
                image, problem = None, problem or (
                    f"image is larger than {MAX_ANNOTATION_IMAGE_BYTES // (1024 * 1024)} MB")
            if image is not None and not image:
                image, problem = None, problem or "image data is empty"
        if kind == "image" and image is None:
            problem = problem or "an image markup needs image data"

        align = str(data.get("align") or 0).lstrip("-")
        return cls(
            kind=kind,
            page=page,
            rect=rect,
            points=tuple(points),
            text=str(data.get("text") or ""),
            color=color,  # type: ignore[arg-type]
            width=_clamp(data.get("width"), 0.1, 80.0, 2.0),
            opacity=_clamp(data.get("opacity"), 0.02, 1.0, 1.0),
            fill_opacity=_clamp(data.get("fill_opacity"), 0.0, 1.0, 0.0),
            size=_clamp(data.get("size"), 2.0, 400.0, 12.0),
            font=(str(data["font"]) if str(data.get("font") or "") in BASE14_LABEL else None),
            align=max(0, min(2, int(align))) if align.isdigit() else 0,
            bold=bool(data.get("bold")),
            italic=bool(data.get("italic")),
            image=image,
            index=index,
            problem=problem,
        )


def _text_width(text: str, choice: FontChoice, fonts: "_FontResolver", size: float) -> float:
    width = fonts.measure(choice, text, size)
    if width is not None:
        return width
    try:
        return float(pymupdf.get_text_length(text, fontname=choice.base14 or "helv",
                                              fontsize=size))
    except Exception:
        return len(text) * size * 0.5


def _wrapped_height(text: str, choice: FontChoice, fonts: "_FontResolver",
                    size: float, width: float) -> float:
    """Height a text box needs once its paragraphs are wrapped to `width`."""
    lines = 0
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        current, count = "", 0
        for word in words:
            trial = f"{current} {word}".strip()
            if current and _text_width(trial, choice, fonts, size) > width:
                count, current = count + 1, word
            else:
                current = trial
        lines += max(1, count + (1 if current else 0))
    return lines * size * 1.32


def _draw_text_annotation(page: pymupdf.Page, ann: Annotation, page_index: int,
                          fonts: "_FontResolver") -> tuple[str | None, FontChoice | None, list[str]]:
    """Draw a new text box, growing its height until the text fits.

    ``insert_textbox`` writes *nothing* when the text does not fit and returns a
    negative spare height (verified against rendered pixels, not assumed), so
    retrying with a taller box is safe rather than a way to duplicate text.
    """
    text = _clean(ann.text)
    if not text.strip():
        return None, None, ["the text box is empty"]
    rect = pymupdf.Rect(ann.rect)
    if ann.font:
        choice = FontChoice(BASE14_LABEL.get(ann.font, ann.font), "requested",
                            "the client asked for this font", base14=ann.font)
    else:
        # "Match the document": inherit the face of the nearest text on the page,
        # exactly as the text editor does when it adds text beside a paragraph.
        spans = collect_spans(page, page_index)
        found = find_text(spans, rect)
        choice = fonts.resolve(page, page_index, text,
                               name=found["defaults"].get("font_name"),
                               bold=ann.bold or None, italic=ann.italic or None)
    fontname = fonts.register(page, page_index, choice)
    width = max(rect.width, ann.size * 1.5)
    height = max(rect.height, _wrapped_height(text, choice, fonts, ann.size, width))
    box = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + height)
    warnings: list[str] = []
    for _ in range(4):
        spare = page.insert_textbox(box, text, fontname=fontname, fontsize=ann.size,
                                    color=ann.color, align=ann.align, render_mode=0)
        if spare >= 0:
            break
        box = pymupdf.Rect(box.x0, box.y0, box.x1, box.y1 + ann.size * 1.6)
    else:
        warnings.append("the text box was grown four times and the text may still be clipped")
    if not _text_present(page, text):
        warnings.append("the text could not be read back from the page after writing")
    return "text", choice, warnings


def _arrow_head(page: pymupdf.Page, ann: Annotation, a: pymupdf.Point,
                b: pymupdf.Point) -> None:
    """The filled triangle at the tip, sized from the stroke width."""
    length = math.hypot(b.x - a.x, b.y - a.y)
    if length < 1.0:
        return
    head = min(max(ann.width * 4.0, 7.0), max(length * 0.6, 7.0))
    angle = math.atan2(b.y - a.y, b.x - a.x)
    left = pymupdf.Point(b.x - head * math.cos(angle - ARROW_HEAD_ANGLE),
                         b.y - head * math.sin(angle - ARROW_HEAD_ANGLE))
    right = pymupdf.Point(b.x - head * math.cos(angle + ARROW_HEAD_ANGLE),
                          b.y - head * math.sin(angle + ARROW_HEAD_ANGLE))
    page.draw_polyline([b, left, right], color=None, fill=ann.color,
                       closePath=True, width=0, fill_opacity=ann.opacity)


def _draw_annotation(page: pymupdf.Page, ann: Annotation, page_index: int,
                     fonts: "_FontResolver", page_box: pymupdf.Rect) -> dict[str, Any]:
    """Draw one markup. `method` is None when nothing was drawn."""
    result: dict[str, Any] = {"method": None, "warnings": [], "color": None, "size": None,
                              "font": None, "font_source": None, "font_note": None}
    rect: pymupdf.Rect | None = None
    if ann.rect:
        rect = pymupdf.Rect(ann.rect) & page_box
        if rect.is_empty or rect.width <= 0.5 or rect.height <= 0.5:
            result["warnings"] = ["this markup falls outside the page"]
            return result

    if ann.kind == "highlight":
        page.draw_rect(rect, color=None, fill=ann.color, width=0,
                       fill_opacity=ann.fill_opacity or DEFAULT_HIGHLIGHT_OPACITY)
        result["method"] = "highlight"
    elif ann.kind in _FILLABLE:
        (page.draw_rect if ann.kind == "rect" else page.draw_oval)(
            rect, color=ann.color, fill=ann.color if ann.fill_opacity > 0 else None,
            width=max(ann.width, 0.2), lineCap=1, lineJoin=1,
            stroke_opacity=ann.opacity, fill_opacity=ann.fill_opacity)
        result["method"] = ann.kind
    elif ann.kind == "image":
        before = len(page.get_images(full=True))
        page.insert_image(rect, stream=ann.image, keep_proportion=False)
        if len(page.get_images(full=True)) <= before:
            result["warnings"] = ["the image was not added to the page"]
            return result
        result["method"] = "image"
    elif ann.kind == "ink":
        page.draw_polyline([pymupdf.Point(*p) for p in ann.points], color=ann.color,
                           fill=None, width=ann.width, lineCap=1, lineJoin=1,
                           stroke_opacity=ann.opacity)
        result["method"] = "ink"
    elif ann.kind in ("line", "arrow"):
        a, b = pymupdf.Point(*ann.points[0]), pymupdf.Point(*ann.points[-1])
        page.draw_line(a, b, color=ann.color, width=ann.width, lineCap=1,
                       stroke_opacity=ann.opacity)
        if ann.kind == "arrow":
            _arrow_head(page, ann, a, b)
        result["method"] = ann.kind
    elif ann.kind == "text":
        method, choice, warnings = _draw_text_annotation(page, ann, page_index, fonts)
        result["method"] = method
        result["warnings"] = warnings
        result["size"] = round(ann.size, 2)
        if choice is not None:
            result["font"] = choice.label or None
            result["font_source"] = choice.source
            result["font_note"] = choice.note

    if result["method"] is not None:
        result["color"] = rgb_to_hex(ann.color)
    return result


def apply_edits(raw: bytes, edits: Sequence[Edit],
                annotations: Sequence[Annotation] = ()) -> tuple[bytes, list[dict[str, Any]]]:
    """Apply every edit and markup to a pristine document.

    Returns ``(pdf_bytes, report)`` where the report holds one row per text edit
    *and* one per markup, so the client can audit everything an export did.

    Stateless on purpose: the caller always passes the original file, so exports
    are idempotent and undo/redo costs nothing server-side.
    """
    doc = open_document(raw)
    reports: dict[int, dict[str, Any]] = {}

    def note(edit: Edit, status: str, **extra: Any) -> None:
        entry = {
            "index": edit.index,
            "page": edit.page,
            "status": status,
            "original_text": None,
            "new_text": edit.text if edit.mode == "replace" else None,
            "size": None,
            "color": None,
            "font": None,
            "font_source": None,
            "font_note": None,
            "bold": None,
            "italic": None,
            "baseline": None,
            "method": None,
            "warnings": [],
        }
        entry.update(extra)
        reports[edit.index] = entry

    def note_annotation(ann: Annotation, status: str, **extra: Any) -> None:
        entry = {
            "index": ann.index,
            "page": ann.page,
            "kind": "annotation",
            "annotation": ann.kind,
            "label": ann.label,
            "status": status,
            "original_text": None,
            # A text box has text to show; every other markup only has a label.
            "new_text": (ann.text if ann.kind == "text" else None),
            "detail": ann.describe(),
            "size": None,
            "color": rgb_to_hex(ann.color),
            "font": None,
            "font_source": None,
            "font_note": None,
            "bold": ann.bold,
            "italic": ann.italic,
            "baseline": None,
            "method": None,
            "warnings": [],
        }
        entry.update(extra)
        reports[ann.index] = entry

    # Group by page: all annotations must exist before a single apply_redactions
    # call, and every redaction must land before any new text is drawn.
    by_page: dict[int, list[Edit]] = {}
    for edit in edits:
        if edit.problem:
            note(edit, "invalid", warnings=[edit.problem])
            continue
        if not (0 <= edit.page < doc.page_count):
            note(edit, "page_out_of_range",
                 warnings=[f"page {edit.page} does not exist ({doc.page_count} pages)"])
            continue
        if edit.mode == "replace" and not edit.text.strip():
            edit.mode = "delete"
        by_page.setdefault(edit.page, []).append(edit)

    by_annotation: dict[int, list[Annotation]] = {}
    for position, ann in enumerate(annotations):
        if ann.index < 0:
            # Never let two rows collide in the report when a caller omits indices.
            ann.index = len(edits) + position
        if ann.problem:
            note_annotation(ann, "invalid", warnings=[ann.problem])
            continue
        if not (0 <= ann.page < doc.page_count):
            note_annotation(ann, "page_out_of_range",
                            warnings=[f"page {ann.page} does not exist ({doc.page_count} pages)"])
            continue
        by_annotation.setdefault(ann.page, []).append(ann)

    fonts = _FontResolver(doc)
    for page_index in sorted(set(by_page) | set(by_annotation)):
        page = doc[page_index]
        spans = collect_spans(page, page_index)
        page_box = pymupdf.Rect(page.mediabox)
        page_edits = by_page.get(page_index, ())
        page_annotations = by_annotation.get(page_index, ())

        plans: list[tuple[Edit, Span | None, dict[str, Any]]] = []
        for edit in page_edits:
            rect = pymupdf.Rect(edit.rect)
            if edit.pad:
                rect = pymupdf.Rect(rect.x0 - edit.pad, rect.y0 - edit.pad,
                                    rect.x1 + edit.pad, rect.y1 + edit.pad)
            rect = rect & page_box
            if rect.is_empty or rect.width <= 0.2 or rect.height <= 0.2:
                note(edit, "invalid", warnings=["edit rect falls outside the page"])
                continue

            found = find_text(spans, rect)
            primary: Span | None = None
            matches = [s for _, s in spans_under(spans, rect)]
            if matches:
                primary = matches[0]

            # When nothing matched, borrow the size/font/colour the locator found
            # nearby rather than inventing them from the box height.
            size = edit.size or (primary.size if primary
                                 else found["defaults"]["size"] or max(rect.height * 0.72, 4.0))
            color = edit.color or (primary.color if primary
                                   else tuple(found["defaults"]["color"]))
            bold = primary.bold if primary else found["defaults"].get("bold")
            italic = primary.italic if primary else found["defaults"].get("italic")

            # Which face to draw in: the document's own font where that is
            # possible, the closest installed match otherwise.
            if edit.font:
                choice = FontChoice(BASE14_LABEL.get(edit.font, edit.font), "requested",
                                    "the client asked for this font", base14=edit.font)
            elif edit.mode == "replace" and edit.text.strip():
                # Fall back to the nearest run's face when nothing was found here,
                # so text added beside a paragraph picks up its typeface.
                choice = fonts.resolve(
                    page, page_index, edit.text,
                    name=(primary.font if primary else found["defaults"].get("font_name")),
                    bold=bold, italic=italic)
            else:
                choice = FontChoice("", "none", "text removed, nothing written")

            if not found["found"]:
                warnings = ["no text found at these coordinates"]
                if found["defaults"].get("from") == "nearest":
                    warnings.append("styling was borrowed from the nearest text")
                status = "no_text_found"
            elif found["approx"]:
                warnings = ["coordinates were slightly off; matched the nearest text"]
                status = "applied_approx"
            else:
                warnings = []
                status = "deleted" if edit.mode == "delete" else "applied"

            # The only glyph removal. A slightly inset box avoids clipping the
            # first character of the neighbouring run when rects abut.
            mark = pymupdf.Rect(rect.x0 + 0.15, rect.y0 + 0.15, rect.x1 - 0.15, rect.y1 - 0.15)
            if mark.is_empty or mark.width <= 0:
                mark = rect
            page.add_redact_annot(mark, fill=None, cross_out=False)

            plans.append((edit, primary, {
                "status": status,
                "warnings": warnings,
                "rect": rect,
                # Nothing under the box reads as None, not "", so the client can
                # tell "this was empty" apart from "we found an empty run".
                "found_text": found["text"] if found["found"] else None,
                "found_font": found["defaults"]["font_label"],
                "size": round(size, 2),
                "color": color,
                "bold": bold,
                "italic": italic,
                "baseline": list(_baseline_for(edit, primary, size)),
                "match": primary is not None,
                "choice": choice,
            }))

        if page_edits:
            page.apply_redactions(**REDACT_KWARGS)

        for edit, primary, plan in plans:
            choice: FontChoice = plan["choice"]
            extra: dict[str, Any] = {
                "original_text": plan["found_text"],
                "size": plan["size"],
                # The colour the replacement was actually drawn in, so "did it keep
                # my colour?" is answerable without opening the file.
                "color": rgb_to_hex(plan["color"]),
                "font": choice.label or None,
                "font_source": choice.source,
                "font_note": choice.note,
                # The weight the original run was set in, so the client can show
                # it (and preview with it) instead of inferring it from the name.
                "bold": plan["bold"],
                "italic": plan["italic"],
                "baseline": [round(v, 2) for v in plan["baseline"]],
                "warnings": plan["warnings"],
            }
            if edit.mode == "delete":
                note(edit, plan["status"], method="delete", **extra)
                continue

            text = edit.text
            fontsize = plan["size"]
            point = (plan["baseline"][0], plan["baseline"][1])
            if edit.autofit:
                span_width = (primary.bbox[2] - primary.bbox[0]) if primary else plan["rect"].width
                fontsize = round(_fit_size(text, choice, fonts, fontsize, span_width), 2)
            extra["size"] = round(fontsize, 2)

            try:
                # Registering happens here, after apply_redactions has rewritten the
                # page, so the new font survives into the output.
                fontname = fonts.register(page, page_index, choice)
                _insert_plain(page, point, text, fontname, fontsize, plan["color"])
                # Base-14 + WinAnsi writes are dependable for Latin text; anything
                # else gets verified because insert_text fails silently otherwise.
                if not _latin1_safe(text) and not _text_present(page, text):
                    raise ValueError("base-14 font could not render this text")
                note(edit, plan["status"], method="text", **extra)
            except Exception:
                try:
                    _insert_rich(page, plan["rect"], text, fontsize, plan["color"])
                    ok = _text_present(page, text)
                    extra["warnings"] = plan["warnings"] + (
                        [] if ok else ["text was drawn but could not be re-read for verification"])
                    note(edit, plan["status"] if ok else "written_unverified", method="htmlbox", **extra)
                except Exception as exc:
                    extra["warnings"] = plan["warnings"] + [f"could not write text: {exc}"]
                    note(edit, "insert_failed", method=None, **extra)

        # Markups go on last: apply_redactions() rewrites the content stream and
        # would wipe anything painted before it, and a pen stroke belongs on top
        # of the finished text anyway.
        for ann in page_annotations:
            try:
                drawn = _draw_annotation(page, ann, page_index, fonts, page_box)
            except Exception as exc:
                note_annotation(ann, "insert_failed",
                                warnings=[f"could not draw this markup: {exc}"])
                continue
            warnings = drawn["warnings"]
            if drawn["method"] is None:
                note_annotation(ann, "invalid", warnings=warnings)
                continue
            note_annotation(ann, "applied" if not warnings else "written_unverified",
                            method=drawn["method"], size=drawn["size"], color=drawn["color"],
                            font=drawn["font"], font_source=drawn["font_source"],
                            font_note=drawn["font_note"], warnings=warnings)

    if fonts.embedded_any:
        # Re-inserting a whole font would bloat the file (a 139 KB document went
        # to 272 KB in testing); subsetting it back down gives 20 KB for the same
        # rendering. Nothing to do unless we actually embedded something.
        try:
            doc.subset_fonts()
        except Exception:
            pass

    out = doc.tobytes(garbage=3, deflate=True, deflate_fonts=True)
    report = [reports[i] for i in sorted(reports)]
    doc.close()
    return out, report


def _text_present(page: pymupdf.Page, text: str) -> bool:
    """Did the text we just wrote actually make it into the page?"""
    needle = _clean(text).strip()
    if not needle:
        return True
    return _clean(page.get_text()).replace("\n", " ").find(needle) >= 0
