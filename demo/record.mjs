#!/usr/bin/env node
/**
 * Records the README demo GIF by driving the real app in a real browser.
 *
 * There is no screen-capture API available here, so this drives the bundled
 * Chromium over the DevTools protocol with nothing but Node's standard library
 * -- including its built-in WebSocket. Every click, drag and keystroke below is
 * dispatched as a genuine input event, so the app behaves exactly as it does
 * under a human hand: hover states fire, gestures drag, the server is really
 * called and the export really runs.
 *
 * Two things have to be supplied that a headless browser does not have:
 *
 *   * a pointer. Headless Chromium does not composite an operating-system
 *     cursor, so `demo/overlay.js` draws one that follows the real mouse events
 *     this script dispatches.
 *   * captions. The same overlay provides them.
 *
 * Frames come from `Page.startScreencast`, each stamped with the browser's own
 * clock. Those stamps are what `demo/assemble.py` uses to rebuild a uniform
 * timeline, so a slow frame does not silently stretch the video.
 *
 * Usage:  node demo/record.mjs          (needs the server on :8777)
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const FRAMES = join(ROOT, ".run", "frames");

const BASE = process.env.PDF_EDITOR_URL || "http://127.0.0.1:8777";
const PORT = Number(process.env.CDP_PORT || 9339);

// The page is laid out at exactly this size; 100% zoom, no scrolling.
const VIEWPORT = { width: 1280, height: 800 };

// Headless Chrome reserves a fixed strip of the window for browser chrome. The
// capture comes back as (window - 16) x (window - 151) device pixels, which
// this pairing makes exactly the viewport -- verified by decoding a frame and
// locating the app's header/toolbar boundary, not assumed.
const WINDOW = `${VIEWPORT.width + 16},${VIEWPORT.height + 151}`;

const QUALITY = Number(process.env.CAPTURE_QUALITY || 92);

// --------------------------------------------------------------------------
// DevTools protocol over the WebSocket Node ships with (no dependencies).
// --------------------------------------------------------------------------

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== undefined) {
        const entry = this.pending.get(msg.id);
        if (!entry) return;
        this.pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(`${msg.error.message} (${JSON.stringify(msg.error.data ?? "")})`));
        else entry.resolve(msg.result);
        return;
      }
      for (const fn of this.handlers.get(`${msg.sessionId ?? ""}:${msg.method}`) ?? []) fn(msg.params);
    });
  }

  on(sessionId, method, fn) {
    const key = `${sessionId}:${method}`;
    if (!this.handlers.has(key)) this.handlers.set(key, []);
    this.handlers.get(key).push(fn);
  }

  /** Listen for a single occurrence, then stop listening. */
  once(sessionId, method, fn) {
    const key = `${sessionId}:${method}`;
    const wrapped = (params) => {
      const list = this.handlers.get(key) ?? [];
      const at = list.indexOf(wrapped);
      if (at >= 0) list.splice(at, 1);
      fn(params);
    };
    this.on(sessionId, method, wrapped);
  }

  send(method, params = {}, sessionId) {
    const id = ++this.seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
    });
  }
}

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const cache = join(process.env.USERPROFILE || process.env.HOME || "", "AppData", "Local", "ms-playwright");
  if (existsSync(cache)) {
    for (const entry of readdirSync(cache)) {
      if (!entry.startsWith("chromium-")) continue;
      const exe = join(cache, entry, "chrome-win64", "chrome.exe");
      if (existsSync(exe)) return exe;
    }
  }
  throw new Error("no Chromium found; set CHROME=/path/to/chrome");
}

// --------------------------------------------------------------------------
// Driving the page
// --------------------------------------------------------------------------

const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const easeOut = (t) => 1 - (1 - t) ** 3;
const r2 = (v) => Math.round(v * 100) / 100;

class Recorder {
  constructor(cdp, sessionId) {
    this.cdp = cdp;
    this.sid = sessionId;
    this.cursor = { x: 0, y: 0 };
    this.down = false;
    this.stepMs = 34;      // sample interval for pointer travel
    this.travel = 620;     // default glide duration
    this.frames = [];
  }

  send(method, params = {}) { return this.cdp.send(method, params, this.sid); }

  async evaluate(expression, awaitPromise = false) {
    const result = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || "evaluate failed");
    }
    return result.result.value;
  }

  async json(expression) {
    const text = await this.evaluate(`JSON.stringify(${expression})`);
    return text ? JSON.parse(text) : null;
  }

  async waitFor(expression, label = expression, timeout = 20000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try { if (await this.evaluate(expression)) return true; } catch { /* mid-navigation */ }
      await sleep(120);
    }
    throw new Error(`timed out waiting for ${label}`);
  }

  // -- input ---------------------------------------------------------------

  async mouse(type, x, y, opts = {}) {
    await this.send("Input.dispatchMouseEvent", {
      type,
      x: r2(x),
      y: r2(y),
      button: opts.button ?? (this.down ? "left" : "none"),
      buttons: opts.buttons ?? (this.down ? 1 : 0),
      clickCount: opts.clickCount ?? 0,
    });
  }

  /** Glide the pointer along an eased path, sampled in real time. */
  async moveTo(x, y, ms = this.travel, ease = easeInOut) {
    const from = { ...this.cursor };
    const steps = Math.max(1, Math.round(ms / this.stepMs));
    const started = Date.now();
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const p = ease(t);
      await this.mouse("mouseMoved", from.x + (x - from.x) * p, from.y + (y - from.y) * p);
      const wait = started + ms * t - Date.now();
      if (wait > 0) await sleep(wait);
    }
    this.cursor = { x, y };
  }

  async press(x, y) {
    this.down = true;
    await this.mouse("mousePressed", x, y, { button: "left", buttons: 1, clickCount: 1 });
  }

  async release(x, y, clickCount = 1) {
    this.down = false;
    await this.mouse("mouseReleased", x, y, { button: "left", buttons: 0, clickCount });
  }

  async click(x, y, settle = 300) {
    await this.moveTo(x, y);
    await sleep(70);
    await this.press(x, y);
    await sleep(80);
    await this.release(x, y);
    await sleep(settle);
  }

  async doubleClick(x, y, settle = 400) {
    await this.click(x, y, 60);
    await sleep(60);
    await this.press(x, y);
    await sleep(50);
    await this.release(x, y, 2);
    await sleep(settle);
  }

  async drag(from, to, ms = 800) {
    await this.moveTo(from[0], from[1], this.travel);
    await sleep(120);
    await this.press(from[0], from[1]);
    await sleep(70);
    await this.moveTo(to[0], to[1], ms, easeOut);
    await sleep(110);
    await this.release(to[0], to[1]);
    await sleep(240);
  }

  async wheel(x, y, deltaY, times = 3) {
    await this.moveTo(x, y, 420);
    for (let i = 0; i < times; i++) {
      await this.send("Input.dispatchMouseEvent", {
        type: "mouseWheel", x: r2(x), y: r2(y), deltaX: 0, deltaY, button: "none", buttons: 0,
      });
      await sleep(110);
    }
  }

  async type(text) {
    await this.send("Input.insertText", { text });
  }

  async key(name) {
    const keys = {
      Enter: { code: "Enter", keyCode: 13, text: "\r" },
      Escape: { code: "Escape", keyCode: 27 },
      Delete: { code: "Delete", keyCode: 46 },
    };
    const k = keys[name];
    if (!k) throw new Error(`unmapped key ${name}`);
    const base = { code: k.code, key: name, windowsVirtualKeyCode: k.keyCode, nativeVirtualKeyCode: k.keyCode };
    await this.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
    if (k.text) await this.send("Input.dispatchKeyEvent", { type: "char", text: k.text, key: name });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }

  // -- geometry ------------------------------------------------------------

  /** Viewport box of the first element matching a selector. */
  async box(selector) {
    return this.json(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height, cx: r.x + r.width / 2, cy: r.y + r.height / 2 };
    })()`);
  }

  /** Viewport box of the nth text-layer run whose text contains `needle`. */
  async textBox(needle, nth = 0) {
    return this.json(`(() => {
      const hits = [...document.querySelectorAll(".text-layer span")]
        .filter((s) => (s.dataset.text || "").includes(${JSON.stringify(needle)}));
      const el = hits[${nth}];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height, cx: r.x + r.width / 2,
               cy: r.y + r.height / 2, right: r.right, bottom: r.bottom, text: el.dataset.text };
    })()`);
  }

  async require(selector) {
    const b = await this.box(selector);
    if (!b) throw new Error(`missing element: ${selector}`);
    return b;
  }

  async requireText(needle, nth = 0) {
    const b = await this.textBox(needle, nth);
    if (!b) throw new Error(`missing text run: ${needle}`);
    return b;
  }

  // -- overlaid furniture --------------------------------------------------

  async caption(text) { await this.evaluate(`window.__demo.caption(${JSON.stringify(text)})`); }
  async place(x, y) { await this.evaluate(`window.__demo.place(${x}, ${y})`); }

  /**
   * Every markup's box, in PDF points against the page it sits on.
   *
   * This is the check that catches an annotation that technically exists but is
   * a degenerate sliver on screen -- a drag that ended where it started.
   */
  async markups() {
    return this.json(`(() => {
      const api = window.pdfEditor;
      return [...api.state.annotations.values()].map((a) => {
        const box = api.annotationBox(a);
        const r = a.el ? a.el.getBoundingClientRect() : null;
        return {
          kind: a.kind,
          page: a.page,
          w: +(box[2] - box[0]).toFixed(1),
          h: +(box[3] - box[1]).toFixed(1),
          points: a.points.length,
          screen: r ? [Math.round(r.x), Math.round(r.y), Math.round(r.right), Math.round(r.bottom)] : null,
        };
      });
    })()`);
  }

  /**
   * Everything drawn on the page, in viewport coordinates, with the collisions
   * between them.
   *
   * Two things ruin a demo's composition and both are pure geometry: markups
   * drawn on top of each other, and a *new* text box landed on a line of the
   * document. The second is the worse of the two, because the browser draws the
   * note over the page's own glyphs and the export then prints it straight
   * through them -- the text underneath is never deleted, since only the demo's
   * own edits are. Both are decided by measuring, not by looking.
   */
  async compositionProblems() {
    return this.json(`(() => {
      const rectOf = (el) => {
        const r = el.getBoundingClientRect();
        return [r.x, r.y, r.right, r.bottom];
      };
      const overlap = (a, b) => {
        const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
        const h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
        return w > 0 && h > 0 ? Math.round(w * h) : 0;
      };
      const round = (r) => r.map((v) => Math.round(v));
      const markups = [...window.pdfEditor.state.annotations.values()]
        .map((a) => ({ kind: a.kind, rect: rectOf(a.el) }));
      // A run the demo deliberately rewrote is fair game: the note may cover
      // text that the export deletes anyway.
      const replaced = [...document.querySelectorAll(".overlay .edit")].map(rectOf);
      const runs = [...document.querySelectorAll(".text-layer span")]
        .filter((s) => (s.dataset.text || "").trim())
        .map(rectOf)
        .filter((r) => !replaced.some((e) => overlap(e, r)));

      const problems = [];
      for (let i = 0; i < markups.length; i++) {
        for (let j = i + 1; j < markups.length; j++) {
          if (overlap(markups[i].rect, markups[j].rect) > 0) {
            problems.push(markups[i].kind + " overlaps the " + markups[j].kind
              + ": " + round(markups[i].rect) + " vs " + round(markups[j].rect));
          }
        }
      }
      for (const m of markups) {
        // A highlight or an ellipse is *meant* to lie on text; a text box is new
        // content and belongs on blank paper.
        if (m.kind !== "text") continue;
        let worst = 0;
        for (const run of runs) worst = Math.max(worst, overlap(m.rect, run));
        if (worst > 0) {
          problems.push("the text box sits on the document's own text (" + worst
            + "px2 of " + round(m.rect) + ")");
        }
      }
      return { problems, markups: markups.map((m) => ({ kind: m.kind, rect: round(m.rect) })) };
    })()`);
  }

  /** Console-visible summary of what the demo has produced so far. */
  async state() {
    return this.json(`(() => {
      const s = window.pdfEditor?.state;
      if (!s) return { ready: false };
      return {
        ready: !!s.pdf,
        pages: s.pages.length,
        tool: s.tool,
        edits: s.edits.size,
        annotations: s.annotations.size,
        changes: document.querySelectorAll("#changes .change").length,
        reportRows: document.querySelectorAll("#report .rows > *").length,
        status: document.getElementById("statusText").textContent,
      };
    })()`);
  }

  // -- capture -------------------------------------------------------------

  async startCapture() {
    this.cdp.on(this.sid, "Page.screencastFrame", (p) => {
      const index = this.frames.length;
      const file = `f${String(index).padStart(5, "0")}.jpg`;
      this.frames.push({ file, ts: p.metadata.timestamp });
      writeFileSync(join(FRAMES, file), Buffer.from(p.data, "base64"));
      this.send("Page.screencastFrameAck", { sessionId: p.sessionId }).catch(() => {});
    });
    await this.send("Page.startScreencast", {
      format: "jpeg", quality: QUALITY, everyNthFrame: 1, maxWidth: 1280, maxHeight: 800,
    });
  }

  async stopCapture() {
    await this.send("Page.stopScreencast");
    await sleep(250);
    return this.frames;
  }
}

// --------------------------------------------------------------------------
// The demo itself
// --------------------------------------------------------------------------

async function run(rec) {
  const log = async (label) => {
    const s = await rec.state();
    console.log(`   ${label.padEnd(30)} edits=${s.edits} markups=${s.annotations} rows=${s.changes} report=${s.reportRows}`);
  };

  // ---- 1. load the sample ------------------------------------------------
  await rec.caption("Load the sample document");
  await rec.place(1180, 742);          // off the page, as if the hand just arrived
  await sleep(520);
  const trySample = await rec.require("#sampleBtn2");
  await rec.click(trySample.cx, trySample.cy, 150);
  await rec.waitFor(
    "window.pdfEditor?.state?.pdf?.numPages >= 3 && window.pdfEditor.state.pages.every((p) => p.canvas.width > 0)",
    "the sample document to render",
  );
  await sleep(900);
  await log("sample loaded");

  // ---- 2. hover: the PDF's real glyph boxes -------------------------------
  const title = await rec.requireText("Quarterly Operations Report");
  const summaryBody = await rec.requireText("Throughput improved");
  const panelHead = await rec.requireText("Headline metric");
  const panelLine = await rec.requireText("Median job latency fell");
  const bullet = await rec.requireText("Capacity headroom");
  const subtitle = await rec.requireText("Prepared by the Operations Team");

  await rec.caption("Hover any line — these are the PDF's real glyph boxes");
  await rec.moveTo(title.cx, title.cy, 700);
  await sleep(420);
  await rec.moveTo(summaryBody.cx, summaryBody.cy, 560);
  await sleep(420);
  await rec.moveTo(bullet.cx, bullet.cy, 520);
  await sleep(380);

  // ---- 3. retype a line --------------------------------------------------
  await rec.caption("Click a line to retype it in place");
  await rec.click(title.cx, title.cy, 900);
  await log("title editor open");

  await rec.caption("Type the replacement, press Enter");
  await rec.type("FY27 Operations Review");
  await sleep(620);
  await rec.key("Enter");
  await sleep(950);
  await log("title replaced");

  // ---- 4. the toolbar restyles one change --------------------------------
  await rec.caption("Restyle it from the toolbar — the export keeps the colour");
  await rec.click(subtitle.cx, subtitle.cy, 620);
  await rec.type("Prepared by the Operations Team  ·  revised");
  await sleep(320);
  const red = await rec.require('#inkSwatches .swatch-btn[data-color="#d92b2b"]');
  await rec.click(red.cx, red.cy, 780);
  await rec.key("Enter");
  await sleep(900);
  await log("subtitle recoloured");

  // ---- 5. highlighter ----------------------------------------------------
  await rec.caption("The highlighter snaps to the text under the sweep");
  const highlighter = await rec.require('[data-tool="highlight"]');
  await rec.click(highlighter.cx, highlighter.cy, 420);
  const bodyLine = await rec.requireText("retired and the remaining pair");
  await rec.drag(
    [bodyLine.x - 6, bodyLine.cy],
    [bodyLine.right + 6, bodyLine.cy],
    860,
  );
  await sleep(820);
  await log("highlight drawn");

  // ---- 6. arrow, ellipse, text box ---------------------------------------
  await rec.caption("Arrows, ellipses and text boxes travel as geometry");
  const arrowTool = await rec.require('[data-tool="arrow"]');
  await rec.click(arrowTool.cx, arrowTool.cy, 340);
  // A call-out from the blank lower third of the page up to the panel.
  await rec.drag([330, 700], [panelLine.right - 60, panelLine.bottom + 18], 900);
  await sleep(520);

  const ellipseTool = await rec.require('[data-tool="ellipse"]');
  await rec.click(ellipseTool.cx, ellipseTool.cy, 340);
  // Around the whole sentence, not a word: an ellipse reads as "this line".
  await rec.drag(
    [panelLine.x - 9, panelLine.y - 5],
    [panelLine.right + 9, panelLine.bottom + 5],
    820,
  );
  await sleep(520);

  const textTool = await rec.require('[data-tool="text"]');
  await rec.click(textTool.cx, textTool.cy, 300);
  // Bottom right of the page, on blank paper. A note is *new* content, so the
  // two things it must miss are the document's own last line (which ends around
  // y 660, this box starts at 684) and the arrow (whose box ends at x 491, this
  // one starts at 516). Landing it beside the arrow's tail instead put the note
  // through the paragraph's last line and inside the arrow's box.
  await rec.drag([516, 684], [712, 722], 760);
  await rec.type("Reviewed — sign-off Q3");
  await sleep(700);
  await rec.click(170, 762, 560);       // click away: commits the text box
  await log("arrow + ellipse + text box");

  const markups = await rec.markups();
  for (const m of markups) {
    console.log(`   markup ${m.kind.padEnd(9)} page ${m.page} ${m.w}x${m.h}pt`
      + (m.points ? `  ${m.points} points` : "") + `  at ${m.screen}`);
    if (m.w < 6 || m.h < 6) throw new Error(`${m.kind} is a degenerate sliver (${m.w}x${m.h}pt)`);
  }

  // Nothing may be drawn through anything else. This is the check that fails
  // loudly if the storyboard above is ever re-arranged carelessly.
  const composition = await rec.compositionProblems();
  for (const m of composition.markups) {
    console.log(`   ${m.kind.padEnd(9)} on screen at ${m.rect.join(", ")}`);
  }
  if (composition.problems.length) {
    throw new Error(`the drawing collides:\n     - ${composition.problems.join("\n     - ")}`);
  }

  // ---- 7. selection follows the shape ------------------------------------
  await rec.caption("Selection follows the shape, not a bounding box");
  const arrowAnn = await rec.json(`(() => {
    const el = [...document.querySelectorAll(".annot")].find((a) => a.querySelector("svg"));
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { cx: r.x + r.width / 2, cy: r.y + r.height / 2 };
  })()`);
  if (!arrowAnn) throw new Error("the arrow markup was not created");
  await rec.click(arrowAnn.cx, arrowAnn.cy, 950);
  await log("arrow selected");

  // Back to blank paper, out on the margin: the halo, the resize handle and the
  // bin are all part of what selection looks like, but the closing frames are
  // about the export, and leaving them on screen puts a grab handle in the
  // middle of the page for no reason.
  await rec.caption("Clicking empty space deselects");
  await rec.click(140, 700, 520);

  // ---- 8. export ---------------------------------------------------------
  await rec.caption("On export the server deletes the glyphs and redraws every markup");
  const exportBtn = await rec.require("#exportBtn");
  await rec.click(exportBtn.cx, exportBtn.cy, 200);
  await rec.waitFor(
    "document.getElementById('statusText').textContent.startsWith('Exported')",
    "the export report",
    30000,
  );
  await sleep(700);
  await log("exported");

  await rec.caption("One HTML page. One Python backend.");
  await rec.wheel(1112, 560, 260, 3);
  await sleep(1900);
  await log("report shown");
}

// --------------------------------------------------------------------------

async function launchChrome(size) {
  const profile = join(ROOT, ".run", `chrome-demo-${size.w}x${size.h}`);
  rmSync(profile, { recursive: true, force: true });
  mkdirSync(profile, { recursive: true });

  const chrome = spawn(findChrome(), [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-scrollbars",
    "--mute-audio",
    // Keep animation and timers at full rate: this is a recording, not a load test.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    `--window-size=${size.w},${size.h}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  let stderr = "";
  chrome.stderr.on("data", (d) => { stderr += d; });

  let version = null;
  for (let i = 0; i < 140 && !version; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (res.ok) version = await res.json();
    } catch { /* not up yet */ }
    if (!version) await sleep(120);
  }
  if (!version) throw new Error(`Chromium never came up.\n${stderr}`);

  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const cdp = new CDP(ws);

  const downloads = join(ROOT, ".run", "downloads");
  mkdirSync(downloads, { recursive: true });
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });

  return {
    chrome,
    cdp,
    ws,
    browser: version.Browser,
    async close() {
      try { ws.close(); } catch { /* already gone */ }
      chrome.kill();
      await sleep(250);
    },
  };
}

/**
 * The screencast captures the *window* surface, not the emulated viewport, and
 * headless Chrome's reserved chrome strip is not the same across launches. So
 * the window size is corrected until a captured frame is exactly the viewport.
 */
async function calibrate() {
  let size = { w: VIEWPORT.width + 16, h: VIEWPORT.height + 151 };
  let browser = await launchChrome(size);
  for (let attempt = 1; attempt <= 3; attempt++) {
    const session = await inputSession(browser);
    if (!session) break;
    const { surface } = session;
    if (surface.w === VIEWPORT.width && surface.h === VIEWPORT.height) {
      console.log(`chromium: ${browser.browser}`);
      console.log(`window ${size.w}x${size.h} -> capture ${surface.w}x${surface.h} (exact)`);
      return { session, size, browser };
    }
    console.log(`attempt ${attempt}: window ${size.w}x${size.h} -> capture ${surface.w}x${surface.h}; correcting`);
    size = {
      w: size.w + (VIEWPORT.width - surface.w),
      h: size.h + (VIEWPORT.height - surface.h),
    };
    await browser.close();
    browser = await launchChrome(size);
  }
  throw new Error("could not calibrate the capture surface to the viewport");
}

/** Open a page target with the viewport pinned, and learn the capture surface. */
async function inputSession(browser) {
  const { cdp } = browser;
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  await cdp.send("Page.enable", {}, sessionId);
  await cdp.send("Runtime.enable", {}, sessionId);
  // Pinning the viewport is what makes the layout deterministic; the window
  // size is then adjusted so the capture is exactly this size too.
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: 1, mobile: false,
  }, sessionId);

  const first = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no screencast frame arrived")), 8000);
    cdp.once(sessionId, "Page.screencastFrame", (p) => {
      clearTimeout(timer);
      resolve({ w: p.metadata.deviceWidth, h: p.metadata.deviceHeight });
    });
  });
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 40, everyNthFrame: 1 }, sessionId);
  let surface;
  try {
    surface = await first;
  } catch (err) {
    console.log(`   ${err.message}`);
    return null;
  }
  return { cdp, sessionId, rec: new Recorder(cdp, sessionId), surface };
}

async function main() {
  rmSync(FRAMES, { recursive: true, force: true });
  mkdirSync(FRAMES, { recursive: true });

  const calibrated = await calibrate();
  const { session, size, browser } = calibrated;
  const { rec, sessionId } = session;
  await session.cdp.send("Page.stopScreencast", {}, sessionId);

  const loaded = new Promise((resolve) => session.cdp.once(sessionId, "Page.loadEventFired", resolve));
  await session.cdp.send("Page.navigate", { url: `${BASE}/` }, sessionId);
  await loaded;
  await rec.waitFor("Boolean(window.pdfEditor)", "the editor to boot");
  await rec.evaluate(readFileSync(join(HERE, "overlay.js"), "utf8"));
  await rec.evaluate("window.__demo.hide()");

  const view = await rec.json("({ w: innerWidth, h: innerHeight, dpr: devicePixelRatio })");
  console.log(`viewport: ${view.w}x${view.h} @${view.dpr}x`);
  if (view.w !== VIEWPORT.width || view.h !== VIEWPORT.height) {
    throw new Error(`viewport is ${view.w}x${view.h}, expected ${VIEWPORT.width}x${VIEWPORT.height}`);
  }

  // Let the webfonts and first paint settle before anything is recorded.
  await sleep(1400);

  rec.frames = [];
  await rec.startCapture();
  await rec.place(1180, 742);

  const started = Date.now();
  try {
    await run(rec);
  } finally {
    const frames = await rec.stopCapture();
    // The markups' on-screen boxes travel with the recording so the assembler
    // can check the exact region each one occupies, rather than hunting for a
    // colour that the palette may have nudged.
    const markups = await rec.markups().catch(() => []);
    writeFileSync(join(ROOT, ".run", "frames.json"), JSON.stringify({
      viewport: VIEWPORT,
      window: `${size.w}x${size.h}`,
      capturedAt: new Date().toISOString(),
      markups,
      frames,
    }, null, 1));
    console.log(`\nrecorded ${frames.length} frames over ${((Date.now() - started) / 1000).toFixed(1)}s`);
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
