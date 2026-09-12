"""Turn the frames recorded by ``demo/record.mjs`` into the README animation.

Three things make the difference between a 3 MB GIF and a 30 MB one, and all
three are handled here rather than hoped for:

* **A uniform timeline.** Screencast frames arrive when the page repaints, not
  on a clock, so a slow frame would otherwise stretch the video. The browser's
  own timestamps are resampled onto a fixed grid.
* **One shared palette.** Built once from a mosaic of the whole recording and
  forced onto every frame, so unchanged pixels really are identical bytes --
  which is what lets the encoder skip them.
* **Delta frames.** Pillow's writer emits a transparent patch for the part of a
  frame that changed, and merges frames that did not change at all, as long as
  it is handed an identical palette and told not to dispose of the previous
  frame. That is what the ``optimize``/``disposal`` pair below is doing.

``--verify`` re-decodes the result and checks two things that a file size cannot
tell you: that the composited frames match the frames that went in (a delta
landing on the wrong canvas looks fine in metadata and wrong on screen), and
that the demo's own marks are actually in there -- the highlight, the arrow, the
ellipse, the caption bar.

Usage:
    python demo/assemble.py --verify              # -> demo/pdf-editor-demo.gif
    python demo/assemble.py --width 1120 --colors 128
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageSequence, ImageStat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN = ROOT / ".run"
FRAMES = RUN / "frames"
OUT = HERE / "pdf-editor-demo.gif"
OUT_VIDEO = HERE / "pdf-editor-demo.webm"

# Colours the UI actually uses, from index.html. Seeding the palette with these
# keeps flat panels and the accent blue exact instead of an approximation.
SEED = [
    "#f4f5f7", "#ffffff", "#1d2129", "#272c36", "#363c48", "#e9ecf1", "#99a2b3",
    "#3f8cff", "#45c48a", "#f0a83c", "#ff6b6b", "#f0c000", "#d92b2b", "#2563eb",
    "#000000", "#f0f1f5", "#ededf2", "#e6e7ec", "#6b7488", "#c3cad8", "#333a48",
]


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def load_manifest() -> dict:
    path = RUN / "frames.json"
    if not path.exists():
        sys.exit("no .run/frames.json -- run `node demo/record.mjs` first")
    return json.loads(path.read_text())


def resample(frames: list[dict], fps: float, start: float, end: float) -> list[tuple[str, int]]:
    """Map captured frames onto a uniform grid, returning (file, duration_ms).

    Consecutive repeats collapse into a single, longer-held frame -- which is
    what a still moment in the recording actually is, and it costs one frame
    instead of dozens.
    """
    if not frames:
        sys.exit("no frames were captured")
    base = frames[0]["ts"] + start
    times = [(f["ts"], f["file"]) for f in frames]
    last = min(times[-1][0], frames[0]["ts"] + end) if end else times[-1][0]

    step = 1.0 / fps
    slots: list[str] = []
    index = 0
    moment = base
    while moment <= last:
        while index + 1 < len(times) and times[index + 1][0] <= moment:
            index += 1
        slots.append(times[index][1])
        moment += step

    out: list[tuple[str, int]] = []
    for name in slots:
        if out and out[-1][0] == name:
            out[-1] = (name, out[-1][1] + int(round(step * 1000)))
        else:
            out.append((name, int(round(step * 1000))))
    return out


def build_palette(items: list[tuple[str, int]], colors: int, width: int) -> Image.Image:
    """One palette for the whole animation, drawn from its own frames."""
    picks = items[:: max(1, len(items) // 40)][:40]
    thumb_w = max(160, width // 5)
    thumb_h = int(800 * thumb_w / width)
    mosaic = Image.new("RGB", (thumb_w * 8, thumb_h * (len(picks) // 8 + 1)))
    for i, (name, _) in enumerate(picks):
        frame = Image.open(FRAMES / name).convert("RGB").resize(
            (thumb_w, thumb_h), Image.Resampling.LANCZOS)
        mosaic.paste(frame, ((i % 8) * thumb_w, (i // 8) * thumb_h))

    seeded = Image.new("RGB", (len(SEED), 1))
    seeded.putdata([hex_rgb(c) for c in SEED])
    mosaic.paste(seeded.resize((len(SEED) * 40, 40), Image.Resampling.NEAREST), (0, 0))
    return mosaic.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)


def quantize(path: Path, palette: Image.Image, width: int) -> Image.Image:
    frame = Image.open(path).convert("RGB")
    if frame.width != width:
        frame = frame.resize(
            (width, round(frame.height * width / frame.width)), Image.Resampling.LANCZOS)
    # No dithering: flat UI panels compress far better as flat patches, and the
    # palette already contains the exact colours they use.
    out = frame.quantize(palette=palette, dither=Image.Dither.NONE)
    out.putpalette(palette.getpalette())
    return out


def render(items: list[tuple[str, int]], out_path: Path, width: int, colors: int) -> None:
    palette = build_palette(items, colors, width)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, (name, duration) in enumerate(items):
        frames.append(quantize(FRAMES / name, palette, width))
        durations.append(duration)
        if i % 80 == 0:
            print(f"   quantised {i + 1}/{len(items)}", flush=True)

    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        # Leave each frame in place: that is what makes the transparent delta
        # patches composite into a full picture instead of a cleared canvas.
        disposal=1,
    )


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------

def find_ffmpeg() -> str:
    """Playwright's browser bundle ships an ffmpeg; use it when there is no system one."""
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    cache = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "") / \
        "AppData" / "Local" / "ms-playwright"
    if cache.is_dir():
        for entry in sorted(cache.glob("ffmpeg-*")):
            for name in ("ffmpeg-win64.exe", "ffmpeg"):
                exe = entry / name
                if exe.exists():
                    return str(exe)
    found = shutil.which("ffmpeg")
    if found:
        return found
    sys.exit("no ffmpeg found; set FFMPEG=/path/to/ffmpeg")


def encode_video(items: list[tuple[str, int]], out_path: Path, width: int,
                 fps: float, crf: int) -> int:
    """Write a real video next to the GIF.

    A video has one frame rate, so the merged holds are expanded back out: a
    two-second pause becomes forty identical frames rather than one frame with a
    long duration. Timing is therefore identical to the GIF's.
    """
    step = 1000 / fps
    expanded = sum(max(1, round(d / step)) for _, d in items)

    def jpeg(name: str) -> bytes:
        frame = Image.open(FRAMES / name).convert("RGB")
        if frame.width != width:
            frame = frame.resize(
                (width, round(frame.height * width / frame.width)), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=94, subsampling=0)
        return buffer.getvalue()

    command = [
        find_ffmpeg(), "-y", "-loglevel", "error",
        # `-vcodec mjpeg` is required, not optional: this ffmpeg is built with
        # `--disable-everything`, so the image2pipe demuxer has no codec table to
        # probe against and reports an unknown codec without it.
        "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps), "-i", "pipe:0",
        "-c:v", "libvpx", "-b:v", "0", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", str(fps), str(out_path),
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    written = 0
    for name, duration in items:
        data = jpeg(name)
        for _ in range(max(1, round(duration / step))):
            proc.stdin.write(data)
            written += 1
    proc.stdin.close()
    error = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        sys.exit(f"ffmpeg failed:\n{error}")
    print(f"wrote {out_path.relative_to(ROOT)}: "
          f"{out_path.stat().st_size / 1024 / 1024:.2f} MB ({written} frames at {fps:g}fps)")
    return expanded


def probe_video(path: Path) -> tuple[int, str]:
    """Decode the video back with the same ffmpeg and read what it actually contains.

    Frames are decoded to tiny PNGs rather than piped, so the count is an
    independent fact rather than something the encoder was trusted to report.
    """
    ffmpeg = find_ffmpeg()
    header = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True)
    log = (header.stderr or b"").decode(errors="replace")
    size = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", log)
    dimensions = f"{size.group(2)}x{size.group(3)}" if size else "?"

    scratch = RUN / "video-probe"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-y", "-i", str(path),
         "-vf", "scale=160:100", "-f", "image2", str(scratch / "f%05d.png")],
        check=True, capture_output=True)
    return len(sorted(scratch.glob("f*.png"))), dimensions


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def count_where(mask: Image.Image) -> int:
    return int(ImageStat.Stat(mask).sum[0] / 255)


def differing(a: Image.Image, b: Image.Image, tol: int = 24) -> int:
    """How many pixels differ between two frames, judged channel by channel.

    Collapsing the difference to luminance first looks equivalent and is not:
    the blue channel only carries 0.114 of the weight, so a pixel 51 away in
    blue scores well under a threshold of 34 and quietly counts as a match.
    """
    diff = ImageChops.difference(a, b)
    masks = [band.point(lambda v: 255 if v > tol else 0) for band in diff.split()]
    return count_where(ImageChops.lighter(ImageChops.lighter(masks[0], masks[1]), masks[2]))


def verify(items: list[tuple[str, int]], gif_path: Path, width: int,
           markups: list[dict], samples: int = 130) -> bool:
    animation = Image.open(gif_path)

    starts: list[float] = []
    clock = 0.0
    for _, duration in items:
        starts.append(clock)
        clock += duration / 1000
    span = clock

    # Three moments to compare: before any markup existed, near the end, and a
    # later one. The second pair is the noise floor -- if the page is otherwise
    # still, it accounts for nothing and the first pair is attributable to the
    # drawing.
    moments = (5.0, span - 3.0, span - 0.4)
    grabbed: dict[float, Image.Image] = {}

    def source_for(moment: float) -> Image.Image:
        index = max(i for i, start in enumerate(starts) if start <= moment)
        source = Image.open(FRAMES / items[index][0]).convert("RGB")
        if source.width != width:
            source = source.resize(
                (width, round(source.height * width / source.width)), Image.Resampling.LANCZOS)
        return source

    step = max(1, animation.n_frames // samples)
    errors: list[float] = []
    header_ok = header_n = 0
    pill = 0
    elapsed = 0.0
    total = 0.0

    for frame in ImageSequence.Iterator(animation):
        duration = frame.info.get("duration", 0) / 1000
        total += duration
        for moment in moments:
            if moment not in grabbed and elapsed >= moment:
                grabbed[moment] = frame.convert("RGB").copy()
        if animation.tell() % step == 0:
            rgb = frame.convert("RGB")

            source = source_for(elapsed)
            if source.size != rgb.size:
                source = source.resize(rgb.size, Image.Resampling.LANCZOS)
            errors.append(sum(ImageStat.Stat(ImageChops.difference(rgb, source)).mean) / 3)

            # The app's chrome is #1d2129 across the top 52 rows.
            header_n += 1
            if max(ImageStat.Stat(rgb.crop((0, 6, rgb.width, 46))).mean) < 80:
                header_ok += 1

            band = rgb.crop((rgb.width // 2 - 300, rgb.height - 60,
                             rgb.width // 2 + 300, rgb.height - 6))
            if count_where(band.convert("L").point(lambda v: 255 if v < 70 else 0)) > 5000:
                pill += 1
        elapsed += duration

    ok = True
    print(f"verify: {animation.n_frames} frames, {total:.2f}s vs {span:.2f}s intended")
    print(f"  frames      composited output matches the originals: "
          f"mean {sum(errors) / len(errors):.2f}/255, worst {max(errors):.2f}")
    print(f"  chrome      app header present in {header_ok}/{header_n} sampled frames")
    print(f"  overlay     caption bar visible in {pill} sampled frames")
    if errors and sum(errors) / len(errors) > 8:
        ok = False

    if markups and len(grabbed) == len(moments):
        before, after, later = (grabbed[m] for m in moments)
        print("  markups")
        for mark in markups:
            box = mark.get("screen")
            if not box:
                continue
            x0, y0, x1, y1 = box
            drawn = differing(before.crop((x0, y0, x1, y1)), after.crop((x0, y0, x1, y1)))
            noise = differing(after.crop((x0, y0, x1, y1)), later.crop((x0, y0, x1, y1)))
            area = max(1, (x1 - x0) * (y1 - y0))
            verdict = "visible" if drawn > area * 0.02 and drawn > noise * 4 else "NOT VISIBLE"
            if verdict != "visible":
                ok = False
            print(f"    {mark['kind']:10} box {x1 - x0}x{y1 - y0}  "
                  f"{drawn:>6}px changed when drawn, {noise:>4}px of noise since  -> {verdict}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=float, default=20.0,
                        help="output rate; 20, 25, 12.5 and 10 divide GIF centiseconds exactly")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--colors", type=int, default=192)
    parser.add_argument("--start", type=float, default=0.0, help="seconds to skip at the start")
    parser.add_argument("--end", type=float, default=0.0, help="seconds to cut from the end")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--video", action="store_true",
                        help="also write a WebM (needs ffmpeg; Playwright's bundle is used by default)")
    parser.add_argument("--crf", type=int, default=24, help="WebM quality; lower is better")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest()
    items = resample(manifest["frames"], args.fps, args.start, args.end)
    span = sum(d for _, d in items) / 1000
    print(f"timeline: {len(items)} frames after merging, {span:.1f}s at {args.fps:g}fps")

    render(items, args.out, args.width, args.colors)
    size = args.out.stat().st_size
    print(f"wrote {args.out.relative_to(ROOT)}: {size / 1024 / 1024:.2f} MB "
          f"({args.width}px wide, {args.colors} colours)")

    if args.video:
        expected = encode_video(items, OUT_VIDEO, args.width, args.fps, args.crf)
        if args.verify:
            got, size = probe_video(OUT_VIDEO)
            verdict = "ok" if got == expected and size == f"{args.width}x{round(800 * args.width / 1280)}" \
                else "MISMATCH"
            print(f"  video       decodes to {got} frames at {size}, expected {expected} "
                  f"at {args.width}x{round(800 * args.width / 1280)}  -> {verdict}")

    if args.verify:
        if not verify(items, args.out, args.width, manifest.get("markups") or []):
            sys.exit("\nverification failed")


if __name__ == "__main__":
    main()
