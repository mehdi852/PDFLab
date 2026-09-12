# The demo recording

`pdf-editor-demo.gif` and `pdf-editor-demo.webm` are what appears at the top of
the main README. Neither is a mock-up or a storyboard: they are the real app,
driven in a real browser with real input events, ending in a real export.

| | |
| --- | --- |
| `record.mjs` | drives Chromium over the DevTools protocol and captures the frames |
| `overlay.js` | injected into the page: the pointer, click ripples and captions |
| `assemble.py` | frames → GIF and WebM, plus the verification |
| `pdf-editor-demo.gif` | 1280×800, ~35 s, ~1.4 MB, 192 colours |
| `pdf-editor-demo.webm` | the same run as VP8 video, ~0.6 MB |

## Re-recording

Start the server, then:

```bash
node demo/record.mjs          # needs Node 18+ for the built-in WebSocket
python demo/assemble.py --video --verify
```

`record.mjs` finds a Chromium in the Playwright browser cache
(`~/AppData/Local/ms-playwright`) or wherever `CHROME` points, and looks for the
server on `http://127.0.0.1:8777` (`PDF_EDITOR_URL` overrides it). `assemble.py`
needs Pillow (`pip install pillow`) and an ffmpeg for the video — it reuses the
one in the Playwright cache, or `FFMPEG=/path/to/ffmpeg`.

Frames land in `.run/frames/` with their capture timestamps in
`.run/frames.json`, so the recording and the encoding are separate steps: you
can re-encode at a different size or frame rate without re-recording.

```bash
python demo/assemble.py --width 1120 --fps 25 --colors 256
python demo/assemble.py --end 22          # drop the last 22 seconds
```

## Why it is built this way

**Nothing is available to record with.** There is no screen-capture API on the
machine this was built on and no headless browser automation package installed,
so `record.mjs` speaks CDP directly using only Node's standard library —
including the `WebSocket` it has shipped since v21. It also means every
interaction is a genuine input event, so the app cannot tell it apart from a
person.

**A headless browser has no pointer.** Chromium only composites an operating
system cursor in a real window, so a recording would show clicks landing with
nothing visibly clicking. `overlay.js` draws one that follows the `mousemove`
events the recorder dispatches, so it can never drift out of step with the hover
states it is causing, and it picks its shape from the element under it —
an I-beam over text, a crosshair while a drawing tool is armed.

**The capture surface is not the viewport.** Headless Chrome reserves a fixed
strip of the window, and how big it is varies between launches, so the window is
sized by trial: a frame is captured, its true dimensions read back, and the
window corrected until the capture is exactly 1280×800 with no scaling or
cropping. A frame is decoded and the app's header/toolbar boundary measured to
confirm it, rather than assumed.

**Frames do not arrive on a clock.** Screencast frames appear when the page
repaints, so a slow frame would stretch the video. Each carries the browser's
own timestamp, and `assemble.py` resamples them onto a fixed grid, which is why
the output duration matches the recording to within 50 ms.

**Size comes from sharing, not from throwing pixels away.** One palette is built
from a mosaic of the whole recording and forced onto every frame, so unchanged
pixels are identical bytes; frames that did not change at all merge into one
longer-held frame, and frames that changed only where the pointer moved are
written as a transparent patch over the previous one. Dithering is off — flat UI
panels compress far better as flat patches, and the palette already contains the
colours the interface actually uses.

## Verification

`--verify` re-decodes the result rather than trusting the encoder, and exits
non-zero if anything fails:

```
timeline: 452 frames after merging, 35.4s at 20fps
wrote demo/pdf-editor-demo.gif: 1.43 MB (1280px wide, 192 colours)
wrote demo/pdf-editor-demo.webm: 0.55 MB (707 frames at 20fps)
  video       decodes to 707 frames at 1280x800, expected 707 at 1280x800  -> ok
verify: 440 frames, 35.35s vs 35.35s intended
  frames      composited output matches the originals: mean 2.68/255, worst 3.62
  chrome      app header present in 147/147 sampled frames
  overlay     caption bar visible in 146 sampled frames
  markups
    highlight  box 361x13    4486px changed when drawn,    0px of noise since  -> visible
    arrow      box 167x259   1185px changed when drawn,    0px of noise since  -> visible
    ellipse    box 310x22    1452px changed when drawn,   13px of noise since  -> visible
    text       box 196x38     464px changed when drawn,    0px of noise since  -> visible
```

The markup check is the one worth keeping. Asking "is there blue near the
ellipse" turned out to be unanswerable — the document has blue headings, and an
earlier version of the check collapsed each channel difference to luminance
before thresholding, which let a pixel 51 away in blue count as a match, since
blue carries only 0.114 of the luminance. So the recorder now also records the
*on-screen box* of every markup it drew, and the check compares that exact
region before and after, with a second pair of frames to measure how much those
pixels drift anyway. That is a question with a real answer.

## Composition guards

`record.mjs` also refuses to finish a run whose drawing collides with itself or
with the page, which is invisible in a still frame and obvious in a recording:

```
Error: the drawing collides:
     - arrow overlaps the text: 324,447,491,706 vs 450,648,720,684
     - the text box sits on the document's own text (1312px2 of 450,648,720,684)
```

No two markups may overlap, and a *new* text box may not land on a line of the
document: the browser paints the note over the page's own glyphs and the export
then prints it straight through them, because only the demo's own edits delete
the text underneath. Highlights and ellipses are exempt from the second rule —
lying on text is what they are for — and a run the demo deliberately rewrote is
fair game for a note.

That is the check that caught the note as it was first drawn: beside the arrow's
tail, where its box overlapped the arrow's by 41×36px and its text sat on the
last line of the Costs paragraph. The note now goes to blank paper at the bottom
of the page, clear of both.
