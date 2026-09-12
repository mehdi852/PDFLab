# PDFLab

**A click-to-edit PDF editor — retype any line in the document, draw on it with
the markup toolbar, and export a real, edited PDF.**

![The editor in action: the sample document loads, a heading is retyped in
place, a subtitle is recoloured from the toolbar, a body line is highlighted, an
arrow, an ellipse and a new text box are drawn, the selection follows the shape
and is cleared again, and the export report audits every change](demo/pdf-editor-demo.gif)

> 📦 **[View this starter on Wholesaas](https://wholesaas.com/scripts)** — the
official product page with a live demo, docs and download.

That recording is not a mock-up — it is the real app being driven in a real
browser with real mouse and keyboard events, ending in a real export. See
[the demo folder](demo/README.md) for how it is made and how to re-record it.

Built by **[wholesaas.com](https://wholesaas.com)** — see the
[PDFLab product page](https://wholesaas.com/scripts) or browse the site for more
ready-to-ship SaaS scripts and starters.

Click any word in a PDF and retype it, or draw on it with the markup toolbar:
pen, highlighter, arrows, shapes, text boxes, images. One HTML page, one Python
backend.

Everything you see is a preview drawn *over* the PDF — the file itself is never
touched in the browser. On export the coordinates travel to the server, which
finds the original text under them, **deletes those glyphs**, writes your
replacement on the same baseline, and redraws every markup from its geometry.

## Run it

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      POSIX:  source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app:app --reload --port 8777
```

Open <http://127.0.0.1:8777> and press **Load sample** to get a document that
exercises the awkward cases, or **Open PDF** for your own file.

pdf.js is loaded from a CDN, so the browser needs internet access on first load.
The backend is served from the same origin, so there is no CORS setup and no
second process to run.

## How it works

**The problem.** You cannot simply "change" text in a PDF. There is no text
object to edit — there are glyphs painted into a content stream. Editing means
deleting the original glyphs and drawing new ones in the same place.

**Frontend** (`index.html`, single page). pdf.js renders each page to a canvas
and lays an invisible text layer on top, purely for hover and hit-testing: one
positioned box per run of text. Clicking a box opens an editable overlay at
*exactly* that spot, prefilled with the text, with its background colour sampled
from the pixels underneath so the preview blends in instead of showing a white
patch. Markups get their own layer above it, which stays out of the pointer's
way until there is actually something in it.

Clicking also fires a `resolve` call, so the browser and the server can be
compared *before* anything is exported. If the server reads something different
at those coordinates, the change is flagged in the sidebar rather than silently
corrupting the output.

That call also hands the preview the two things it would otherwise have to guess
at:

* **The typeface.** Every font pdf.js loaded is registered in `document.fonts`
  under the text item's own `fontName`, so the overlay names that first and
  renders in the document's real face. The server's CSS family stack (its
  resolved family for this run, plus a generic) follows as a fallback.
* **The colour.** The overlay takes the ink colour the server resolved for that
  spot rather than sampling a pixel, so preview and export agree by construction.
  When the server is unreachable it falls back to sampling the pixel furthest
  from the background — which, unlike "the darkest pixel", still finds white text
  on a dark panel.

**Backend** (`pdf_ops.py`). Per edit:

1. Find the text under the rect — used to report what was replaced and to
   inherit the original font, size, colour and baseline.
2. Mark the rect as a redaction annotation and remove the glyphs.
3. Draw the new text, on the original baseline, in the original size.

Removing text without damaging the page needs three explicit arguments. The
defaults of `apply_redactions()` would remove vector art under the box and
pixelate images, and `add_redact_annot()` would draw a black cross-out:

```python
page.add_redact_annot(rect, fill=None, cross_out=False)   # fill=None: paint nothing
page.apply_redactions(
    images=pymupdf.PDF_REDACT_IMAGE_NONE,       # don't touch images
    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,  # don't touch vector art
    text=pymupdf.PDF_REDACT_TEXT_REMOVE,        # do delete the glyphs
)
```

`fill=None` is what keeps the page background intact — a white box would show up
immediately on any coloured panel, and the browser preview would stop matching
the export.

**Exports are stateless.** The server always keeps the pristine upload and
re-applies the whole edit list, so exporting twice cannot stack changes, and
undo/redo costs nothing server-side.

### Fonts

A replacement inherits the original's size, **colour** and baseline exactly: the
colour comes from the original run, is sent as null by the client, and is
reported back per edit as a hex value — so "did it keep my colour?" is answerable
without opening the file. Verified in tests for dark-on-light, mid grey, and the
white-on-dark case, comparing both the reported colour and the rendered pixels.

The typeface is chosen in three tiers, and the export report says which tier was
used for every edit:

| | What it means |
| --- | --- |
| **document's own font** | The document is set in one of the standard 14, or its embedded font was re-inserted as-is. This is exact, not an approximation. |
| **closest installed match** | The document's font could not be reused, so the nearest face on this machine was embedded instead, chosen by family name, compatible aliases (Calibri→Carlito, Arial→Liberation Sans) and bold/italic/serif class. |
| **approximated** | Nothing suitable was available; a built-in base-14 face was used. |

Two things make this safe rather than merely optimistic.

**Every candidate is render-tested first.** A scratch document is opened, the font
is inserted and the text is drawn in it, then the result is checked: it must spell
the text correctly *and* the run must report the same font family that was asked
for. Both halves matter, and both were found the hard way:

* A font whose character map was stripped by subsetting **accepts the call and
  emits null bytes** instead of glyphs.
* A font MuPDF cannot embed — a `.ttc` collection, for instance — is **silently
  swapped for a built-in face**. The text still spells correctly, in entirely the
  wrong typeface, which is why spelling alone is not enough of a check.

**Icon and symbol fonts are excluded.** Dingbats map the Latin range to pictures,
so a glyph-coverage test happily accepts Wingdings for an ordinary sentence.

### Weight

Bold and italic are part of the same inheritance: the run's own flags are read
from the PDF, used to pick the face, and reported per edit as `bold`/`italic` so
the client can show and preview them. Two traps are handled explicitly.

* **A run's flags beat its name.** A face can be bold without saying so in its
  name (an embedded copy named plainly "Helvetica", say). Reading the name alone
  picked the regular cut, so the replacement came out lighter than the text it
  replaced.
* **The preview must not fake the weight.** pdf.js registers embedded faces at
  weight 400, so asking for 700 on top of an already-bold face invites a
  synthesised double-bold. The overlay sets `font-synthesis: none`, which lets a
  real bold face through untouched while a system family still gets its own
  genuine bold.

On the client the family and the weight come from different places, for a
reason. pdf.js only registers a web font for the faces it had to *load*; a
standard-14 run is painted from pdf.js's built-in data and has no face under its
`fontName`. Naming that name anyway made the browser fall through to the next
entry in the stack — which is exactly how a bold run ended up previewing in
regular weight. So the overlay uses the pdf.js face only when it is genuinely
registered, and otherwise the server's family stack plus the reported weight.

Re-inserting a whole font would bloat the file (a 139 KB document went to 272 KB
in testing), so when anything was embedded the document is subset afterwards,
which brought that same file down to 21 KB. Subsetting runs only when a font was
actually added, and never substitutes a font it cannot handle.

Fonts are found in the usual system directories, or wherever
`PDF_EDITOR_FONT_DIRS` points.

Latin-1 text is written directly. Anything the chosen face cannot draw — CJK,
emoji — falls through to `insert_htmlbox`, which reaches for a system font and
reports that it did.

### The coordinate contract

Everything on the server uses **PyMuPDF page space**: points, origin top-left, y
growing downward, measured against the **unrotated** page box — *not*
`page.rect`, which reports the rotated display size.

That distinction was established experimentally against rendered pixels: on a
page with `/Rotate 90`, `page.rect` is the rotated size while `get_text()` and
`insert_text()` both keep using unrotated space. Sticking to unrotated space
means one coordinate system covers every rotation, and the server needs no
derotation maths.

The browser works in display space (pdf.js applies `/Rotate` to its viewport) and
converts before sending, using pdf.js's own `convertToPdfPoint`. The hit boxes
are built from each run's true quad — baseline direction and ascent direction
from the text transform — rather than assuming horizontal text, which is what
makes rotated pages work.

**The client sends geometry and text; the server owns typography.** Edits carry
`font`/`size`/`color` as null, so the exported file cannot drift from the
original document's styling.

### Markup tools

The toolbar draws things that are not text: freehand ink, highlighter bands,
arrows, rectangles, ellipses, new text boxes and inserted images. Eight tools,
it remembers a separate colour and width for each, and it is contextual — the
controls describe the selected markup, or the brush the next one will use.

**Markups are geometry, like text edits are.** The browser paints them for
feedback (DOM for boxes and text, SVG for strokes) and sends coordinates; the
server redraws them from those coordinates with `draw_polyline`, `draw_rect`,
`draw_oval` and `insert_image`. Nothing about the export depends on how the
browser happened to render the preview.

The backend draws markups **after** the redaction pass for that page. That
ordering is not cosmetic: `apply_redactions()` rewrites the page's content stream
and would wipe anything painted before it, so a highlight and a text edit on the
same page must be sequenced, not merged.

Geometry is in the same PDF-point space as the text edits, so the two never
disagree about where something sits:

* **Boxes** (highlight, rectangle, ellipse, image, text box) carry a `rect`.
* **Strokes** (pen, line, arrow) carry a list of `points`.
* An arrow's head is computed in **PDF points** and converted for display, so the
  preview's arrowhead and the exported one cannot drift apart.

#### The highlighter snaps to the text

Dragging *along* a line is the normal highlighter gesture, which means a
perfectly flat drag with zero height — a rectangle the export would rightly
reject as empty. So a highlight sweep snaps to the text-layer boxes underneath
it: the band takes its height from the glyphs and its horizontal extent from the
sweep. A sweep across several lines emits **one band per line**, and bands never
keep pdf.js's run boundaries (a three-word sweep would otherwise highlight a
whole line). Nothing is snapped if the sweep catches no text — it becomes a thin
band where you dragged.

#### Text boxes

A new text box has its own typography, unlike a text *replacement* which
inherits the document's: family (Helvetica, Times, Courier, or **Match document**
— resolved server-side from the nearest text on the page), size, bold, italic,
colour and alignment.

A text box **grows to fit its text** instead of dropping it. `insert_textbox`
writes nothing at all when the text does not fit and returns a negative spare
height (verified against rendered pixels rather than assumed), so the box height
is recomputed from a real measurement and the call retried. The preview wraps to
the same width.

Typing updates the model on every keystroke, so exporting mid-edit sends what is
on screen. A text box left empty is dropped when you click away — the export
would only report an empty box as invalid.

#### Images

**Choose image…** reads a PNG, JPEG or WebP (up to 6 MB), then the next drag
places it. A dragged box keeps the image's proportions instead of stretching it;
a plain click places it at a sensible default width. The bytes are passed through
untouched — no re-encoding — and the server caps a single image at 12 MB and all
images in one export at 48 MB.

#### Hit-testing, selection and editing

The cursor tool is the default and it does two jobs at once. Hovering a run of
text shows an I-beam and a tint, and clicking it opens the editor in place;
clicking a markup selects it instead.

With a markup selected: drag to move it, drag the corner handle to resize it,
double-click a text box to edit its text, press `Delete` (or the toolbar's bin)
to remove it. Strokes get a few pixels of clickable halo, so a thin pen line is
not a game of pixel hunting, and resizing rescales a stroke's actual points —
the ink follows the box, not just its frame.

**Selection follows the shape, never a bounding box.** A dashed rectangle round
a diagonal arrow or a circle is the kind of thing that makes an editor feel
homemade, so nothing is drawn at all until you select something, and then:

| | Selected by |
| --- | --- |
| pen, line, arrow | a translucent, wider copy of the stroke itself, drawn underneath — plus the same for an arrow's head |
| ellipse | an elliptical ring |
| rectangle, highlight, image, text box | a rectangular ring, because that *is* the shape |

The ring is its own element rather than a CSS `outline`, so the elliptical case
does not depend on the browser rounding a `border-radius`. The halo is built
from the same `path` data as the artwork in the same function, so it cannot drift
from the stroke it belongs to — at any zoom or after any resize.

While you are still dragging, the preview follows the shape for the same reason:
the ellipse tool draws a dashed *ellipse*, not a dashed rectangle drawn around
one. The preview borrows the shape's own rounding, so the two cannot disagree
about what is being drawn.

**Clicking outside deselects.** The empty part of a page, the grey margin, or
another object — anything that is not a markup, a run of text or an edit box —
ends the selection, and commits whatever text box was still being typed in. The
sidebar list and the page agree: clicking a row selects that object, and the
selected row is highlighted.

A text *replacement* is a selectable object too, which is what makes the
toolbar's typeface, size and colour controls apply to it: pick a face or a
colour for one change without touching the rest of the document, and **Auto
style** to hand it back to the document's own styling.

One trap worth recording: the markup layer spans every page, so without
`pointer-events: none` on the *layer* (with `auto` on the markups themselves) an
empty `div` sits above the text layer and swallows every hover and click meant
for the text. It is invisible in the DOM and only shows up under real mouse
input, because a synthetically dispatched event bypasses hit-testing entirely.

While a drawing tool is active the page becomes a canvas: the text layer, the
edit boxes and existing markups all stop taking the pointer, so you cannot
accidentally retype a paragraph while trying to draw over it. One-shot tools
(text, image, arrow, shapes) hand the pointer back after one object; the pen and
the highlighter stay active, because drawing several strokes in a row is how they
are used.

Keyboard: `V` select, `T` text, `I` image, `P` pen, `H` highlighter, `A` arrow,
`R` rectangle, `O` ellipse, `Delete` remove, `Escape` deselect.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | the editor page |
| `GET` | `/api/health` | liveness + PyMuPDF version |
| `POST` | `/api/documents` | upload a PDF (multipart) → id, page geometry |
| `GET` | `/api/documents/{id}/file` | the pristine original, for pdf.js |
| `GET` | `/api/sample` | upload a generated demo document |
| `POST` | `/api/documents/{id}/resolve` | what text does the server see under this rect? |
| `POST` | `/api/documents/{id}/export` | apply the edits → base64 PDF + per-edit report |

An edit is `{page, rect: [x0, y0, x1, y1], text, mode, autofit}` plus optional
`font`, `size`, `color` and `align` when the user asked for specific typography
(leave them null to inherit the document's). Empty text with `mode: "delete"`
removes text; that is what clearing a box and pressing Enter sends.

A markup is `{kind, page, rect | points, color, width, opacity, fill_opacity}`,
with `text`/`size`/`font`/`align` for a text box and a base64 `image` for an
image. `kind` is one of `text`, `image`, `ink`, `line`, `arrow`, `rect`,
`ellipse`, `highlight`. Colours are `[r, g, b]` in 0..1.

One request carries both: `{"edits": [...], "annotations": [...]}`. Markups are
report rows too, with `kind: "annotation"`, a `label`, a human-readable `detail`,
the colour actually drawn, and for a text box its font and the usual font-source
fields — so a single export report audits text changes and drawings together.

The export report tells you what happened to *each* edit — `applied`,
`applied_approx`, `deleted`, `no_text_found`, `insert_failed`, `invalid`,
`page_out_of_range` — including the font and size that were inherited, so an
edit that could not be verified never disappears silently.

## The recording above

Every click, drag and keystroke in it is dispatched to a real browser over the
DevTools protocol, so the app behaves exactly as it does under a human hand:
hover states fire, drags drag, the server is really called and the export really
runs. Two things have to be supplied that a headless browser does not have — a
visible pointer and captions — so `demo/overlay.js` draws both, driven by the
same mouse events the recorder dispatches rather than by a separate animation,
which is what keeps the pointer in step with the hover states it causes.

Frames come from Chromium's screencast, resampled onto a fixed timeline using
the browser's own timestamps, then quantised against one palette shared by every
frame and encoded as delta patches. The result is 35 seconds of interface at
1280×800 in 1.4 MB. A 0.6 MB WebM is written alongside it, which is the better
choice if you are embedding it somewhere that takes real video.

`python demo/assemble.py --verify` re-decodes the GIF and checks it: that the
composited frames match the frames that went in, and that each markup really
occupies its own screen box — comparing that box before and after the drawing,
with a second pair of frames to measure the noise floor. Colour matching alone
was not enough to trust: an early version of that check collapsed the difference
to luminance, and the blue channel's 0.114 weight was enough to count the
document's own headings as the ellipse. While it drives the app, the recorder
also refuses to record a run in which two markups overlap or a new text box
lands on a line of the document — both fail in a way no still frame shows.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

111 tests across four files. The engine (`tests/test_pdf_ops.py`) covers glyph
removal, artwork and background survival, coordinate behaviour at 0/90/180/270°,
CJK fallback and malformed input. The API (`tests/test_api.py`) covers every
endpoint and error path, and round-trips an exported file back through the server
to confirm the replacement text is really in it. `tests/test_fonts.py` covers
font reuse, the closest-match fallback, name normalisation, and the two silent
failure modes described above. `tests/test_markups.py` renders each markup kind
and counts pixels — that a highlight is genuinely translucent and leaves the text
under it readable, that an arrow has a head at its tip and not along its shaft,
that a text box grows instead of vanishing, that an image lands in its box and
nowhere else, and that a markup painted under the redaction pass does not
disappear.

## Known limits

- **Markups are baked into the page, not PDF annotations.** They are drawn into
the content stream, so the exported file shows them everywhere but they are not
separate objects you can select and move in Acrobat afterwards. That is the price
of not depending on any reader's annotation support.
- **No undo stack.** Removing one thing at a time and **Clear all** are what you
get. Neither touches the original upload, so re-exporting is always safe.
- **A highlight band is axis-aligned.** On a rotated page the band is a
rectangle in unrotated page space, so it follows the text rather than slanting
with the display.
- **Markups always sit on top.** A pen stroke can cover text; there is no "send
backwards".
- **One line at a time.** Each editable box is a run of text on one line.
  Replacing a paragraph does not reflow the ones below it.
- **Scanned pages.** With no text layer there is nothing to delete. Your text is
  still written and the export flags it as unverified, rather than discarding
  your work.
- **A font that cannot be reused falls back gracefully.** Heavily subset fonts
  lose their character map and cannot accept new text at all; those replacements
  get the closest installed face of the same family, or a base-14 approximation,
  and the report says which. Reuse is also skipped for fonts above 8 MB.
- **Only a handful of fonts are searched for non-Latin text.** Matching tests the
  top ~24 candidates, which covers Latin text comfortably; CJK and similar go
  straight to MuPDF's own font fallback, which is correct but not the document's
  real typeface.
- **The preview approximates typography; the export is exact.** The overlay is
  positioned from the real glyph box, but the browser's font metrics differ
  slightly from the PDF's.
- **Uploads live in memory**, capped at 64 MB per file with the 24 most recent
  kept; a restart clears them. Password-protected PDFs are rejected.
- **Unlike the text editor, markups are not verified against the page.** A text
  edit is checked back after writing (`applied` vs `written_unverified`); a
  vector markup is reported by construction, because `draw_*` either commits or
  raises. Text boxes and images *are* read back from the page.


## License and where this comes from

PDFLab is part of the **[Wholesaas](https://wholesaas.com)** library — free,
production-ready scripts and starters you can download, keep and ship
commercially. Browse [the full library](https://wholesaas.com/scripts), or start
with [NextStarter](https://wholesaas.com/scripts/nextstarter-production-ready-saas-starter-next-js-16-supabase-stripe)
if you want the SaaS foundation — auth, teams, Stripe billing, admin — around a
product like this one.

Licensed under the [Wholesaas Commercial License](LICENSE): free to use, modify
and ship in your own products, monetised or not; not to be resold or
redistributed as a script. The authoritative version of the license lives at
<https://wholesaas.com/licenses>.
