/**
 * Cursor, click-ripple and caption overlay for the README demo.
 *
 * Injected by `demo/record.mjs` -- never by the app itself. Headless Chromium
 * does not composite an operating-system pointer, so a screen recording would
 * otherwise show clicks happening with nothing visible doing the clicking.
 *
 * The pointer is driven by the *real* mouse events the recorder dispatches, not
 * by a separate animation: the sprite listens on `mousemove`, so it can never
 * drift out of step with the hover states it is supposed to be causing. The
 * same event supplies the click position for the ripple.
 *
 * The sprite shape is taken from the element under the pointer's computed
 * `cursor`, so the editor's own affordances show through: an I-beam over text,
 * a crosshair while a drawing tool is armed.
 */
(() => {
  if (window.__demo) return;

  const Z = 2147483000;
  const NS = "http://www.w3.org/2000/svg";

  const ARROW_HOTSPOT = [0, 0];
  const CENTRED = (size) => [-size / 2, -size / 2];

  // Each sprite is drawn with a white halo underneath so it stays legible over
  // both the light page and the dark chrome.
  const SPRITES = {
    arrow: {
      hotspot: ARROW_HOTSPOT,
      size: [15, 25],
      paths: [
        { d: "M1 1 L1 17.8 L5 14.1 L7.6 20 L10.2 18.9 L7.6 13 L12.9 13 Z",
          fill: "#ffffff", stroke: "#161a21", width: 1.3 },
      ],
    },
    text: {
      hotspot: CENTRED(23),
      size: [17, 23],
      paths: [
        { d: "M3.5 2.6 h10 M3.5 20.4 h10 M8.5 2.6 v17.8", fill: "none", stroke: "#ffffff", width: 4 },
        { d: "M3.5 2.6 h10 M3.5 20.4 h10 M8.5 2.6 v17.8", fill: "none", stroke: "#161a21", width: 1.5 },
      ],
    },
    crosshair: {
      hotspot: CENTRED(22),
      size: [22, 22],
      paths: [
        { d: "M11 1.6 V7.4 M11 14.6 V20.4 M1.6 11 H7.4 M14.6 11 H20.4", fill: "none", stroke: "#ffffff", width: 3.6 },
        { d: "M11 1.6 V7.4 M11 14.6 V20.4 M1.6 11 H7.4 M14.6 11 H20.4", fill: "none", stroke: "#161a21", width: 1.3 },
      ],
    },
    move: {
      hotspot: CENTRED(23),
      size: [23, 23],
      paths: [
        { d: "M11.5 1.9 l3.2 3.9 h-6.4 z M11.5 21.1 l3.2 -3.9 h-6.4 z"
           + " M1.9 11.5 l3.9 -3.2 v6.4 z M21.1 11.5 l-3.9 -3.2 v6.4 z",
          fill: "#ffffff", stroke: "#161a21", width: 1.2 },
        { d: "M11.5 5.2 V17.8 M5.2 11.5 H17.8", fill: "none", stroke: "#161a21", width: 1.5 },
      ],
    },
  };

  // The app's own `cursor` values, mapped onto the sprites above. Anything not
  // listed (pointer, default, ...) gets the plain arrow.
  const CURSOR_KIND = {
    text: "text",
    crosshair: "crosshair",
    move: "move",
    grab: "move",
    "-webkit-grab": "move",
    all: "move",
    "nwse-resize": "move",
    "nesw-resize": "move",
    "ns-resize": "move",
    "ew-resize": "move",
    "col-resize": "move",
    "row-resize": "move",
  };

  function buildSprite(kind) {
    const spec = SPRITES[kind];
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "d-sprite d-sprite-" + kind);
    svg.setAttribute("width", String(spec.size[0]));
    svg.setAttribute("height", String(spec.size[1]));
    svg.setAttribute("viewBox", `0 0 ${spec.size[0]} ${spec.size[1]}`);
    svg.style.cssText = `position:absolute;left:${spec.hotspot[0]}px;top:${spec.hotspot[1]}px;`
      + "overflow:visible;display:none;"
      + "filter:drop-shadow(0 1px 1.6px rgba(10,14,22,.42));";
    for (const p of spec.paths) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", p.d);
      path.setAttribute("fill", p.fill);
      if (p.stroke) {
        path.setAttribute("stroke", p.stroke);
        path.setAttribute("stroke-width", String(p.width));
        path.setAttribute("stroke-linejoin", "round");
        path.setAttribute("stroke-linecap", "round");
      }
      svg.append(path);
    }
    return svg;
  }

  const style = document.createElement("style");
  style.textContent = `
    #__demo-cursor, #__demo-caption, .d-ripple {
      position: fixed; pointer-events: none; margin: 0; padding: 0; border: 0;
      box-sizing: border-box;
    }
    #__demo-cursor { left: 0; top: 0; z-index: ${Z + 3}; will-change: transform; }
    .d-ripple {
      z-index: ${Z + 2}; border-radius: 50%; transform: translate(-50%, -50%);
      border: 2px solid rgba(63, 140, 255, .95);
      background: rgba(63, 140, 255, .18);
    }
    #__demo-caption {
      left: 50%; bottom: 20px; z-index: ${Z + 1};
      transform: translateX(-50%) translateY(7px);
      max-width: 76%; white-space: nowrap; opacity: 0;
      background: rgba(15, 18, 24, .9); color: #fff;
      font: 500 13.5px/1.25 "Segoe UI", system-ui, -apple-system, sans-serif;
      letter-spacing: .15px; padding: 9px 17px; border-radius: 999px;
      box-shadow: 0 10px 30px rgba(9, 12, 18, .34), 0 0 0 1px rgba(255, 255, 255, .07);
      transition: opacity .24s ease, transform .24s ease;
    }
    #__demo-caption.on { opacity: 1; transform: translateX(-50%) translateY(0); }
  `;

  const cursor = document.createElement("div");
  cursor.id = "__demo-cursor";
  for (const kind of Object.keys(SPRITES)) cursor.append(buildSprite(kind));

  const caption = document.createElement("div");
  caption.id = "__demo-caption";

  const overlay = document.createElement("div");
  overlay.id = "__demo-root";
  overlay.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:" + Z;

  overlay.append(cursor, caption);
  document.documentElement.append(style, overlay);

  let kind = null;
  let last = { x: 0, y: 0 };

  function show(next) {
    if (next === kind) return;
    kind = next;
    for (const el of cursor.children) {
      el.style.display = el.classList.contains("d-sprite-" + next) ? "block" : "none";
    }
  }

  /** Which sprite suits the element under this point, according to the app's own CSS. */
  function kindAt(x, y) {
    let el = null;
    try { el = document.elementFromPoint(x, y); } catch { /* outside the viewport */ }
    if (!el) return "arrow";
    const value = getComputedStyle(el).cursor || "";
    return CURSOR_KIND[value] || "arrow";
  }

  let visible = true;

  function place(x, y) {
    last = { x, y };
    if (!visible) { visible = true; cursor.style.display = "block"; }
    cursor.style.transform = `translate(${x}px, ${y}px)`;
    show(kindAt(x, y));
  }

  function ripple(x, y) {
    const el = document.createElement("div");
    el.className = "d-ripple";
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    el.style.width = el.style.height = "6px";
    overlay.append(el);
    const anim = el.animate(
      [
        { transform: "translate(-50%, -50%) scale(.6)", opacity: 0.9 },
        { transform: "translate(-50%, -50%) scale(5.4)", opacity: 0 },
      ],
      { duration: 560, easing: "cubic-bezier(.22,.61,.36,1)" },
    );
    anim.onfinish = () => el.remove();
  }

  // Capture phase: the app calls stopPropagation() in several handlers, and the
  // pointer must keep tracking regardless.
  addEventListener("mousemove", (e) => place(e.clientX, e.clientY), true);
  addEventListener("mousedown", (e) => ripple(e.clientX, e.clientY), true);

  window.__demo = {
    place,
    ripple,
    caption(text) {
      caption.textContent = text || "";
      caption.classList.toggle("on", Boolean(text));
    },
    /** Hide the pointer -- used while the app is still loading. */
    hide() {
      visible = false;
      cursor.style.display = "none";
      kind = null;
    },
    get position() { return last; },
  };
})();
