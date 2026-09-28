"""Record the README hero GIF — a scripted, Screen-Studio-style demo of the web UI.

    uv run python scripts/make_demo_gif.py [--db PATH] [--outputs DIR] [--query adani] [--pick ADANIPOWER]

1. **Record.** Starts the web server in-process (against ``--db``, ideally a copy with the
   watchlist emptied so no personal data shows), drives it in headless Chromium with Playwright —
   type a group name, click one of the numbered buttons, wait for the deep report, scroll into it —
   with an injected cursor + click ripple, and logs when and where each action happened.
2. **Retime.** The multi-minute report build is sped through (~40×); interactions stay real-time.
3. **Auto-zoom.** Every moment eases toward the region the action happened in (the search box while
   typing, the buttons, the report), like Screen Studio / Recordly — but deterministic, so the GIF is
   re-generated with one command whenever the UI changes.
4. **Export.** ``docs/media/demo.gif`` (palette-optimised) and ``docs/media/demo.mp4``.

Needs ffmpeg on PATH (and Pillow). Uses your .env LLM for the report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

W, H = 1280, 800                  # recorded viewport
OUT_W = 960                       # GIF width (keeps it small enough for a README)
FPS = 15

CURSOR_JS = r"""
(() => {
  const arrow = `data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='26' height='26' viewBox='0 0 26 26'><path d='M4 2 L4 21 L9 16 L12.5 24 L16 22.5 L12.6 14.8 L19.5 14.8 Z' fill='black' stroke='white' stroke-width='1.6' stroke-linejoin='round'/></svg>`;
  const mount = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div');
    c.id = '__cur';
    Object.assign(c.style, {position: 'fixed', left: '-40px', top: '-40px', width: '26px', height: '26px',
      background: `url("${arrow}") no-repeat`, pointerEvents: 'none', zIndex: 2147483647,
      transform: 'translate(-3px,-1px)'});
    document.documentElement.appendChild(c);
    document.addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    document.addEventListener('mousedown', (e) => {
      const r = document.createElement('div');
      Object.assign(r.style, {position: 'fixed', left: (e.clientX - 18) + 'px', top: (e.clientY - 18) + 'px',
        width: '36px', height: '36px', borderRadius: '50%', border: '3px solid rgba(15,118,110,.85)',
        pointerEvents: 'none', zIndex: 2147483646, transition: 'transform .45s ease-out, opacity .45s ease-out'});
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.9)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 500);
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
"""


@dataclass
class Beat:
    """A stretch of the recording: [t0, t1) in source seconds, played at ``speed``, framed on ``rect``
    (x, y, w, h in viewport px; None = the whole screen)."""
    t0: float
    t1: float
    speed: float
    rect: tuple[float, float, float, float] | None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def record(query: str, pick: str, workdir: Path) -> tuple[Path, list[Beat]]:
    """Drive the UI and return (video file, beats)."""
    from playwright.sync_api import sync_playwright

    from equity_research.web import server as web

    port = _free_port()
    srv = web._server("127.0.0.1", port)
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    marks: list[tuple[str, float, tuple | None]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="light",
                                  record_video_dir=str(workdir), record_video_size={"width": W, "height": H})
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        t0 = time.monotonic()

        def mark(name: str, box=None) -> None:
            rect = (box["x"], box["y"], box["width"], box["height"]) if box else None
            marks.append((name, time.monotonic() - t0, rect))

        page.goto(base)
        page.wait_for_selector("#q")
        page.mouse.move(W * 0.62, H * 0.62)
        mark("open")
        time.sleep(1.4)

        box = page.locator(".ask-box").bounding_box()
        mark("type", box)
        page.mouse.move(box["x"] + 120, box["y"] + box["height"] / 2, steps=25)
        page.mouse.down()
        page.mouse.up()
        page.keyboard.type(query, delay=110)
        time.sleep(0.5)
        page.keyboard.press("Escape")
        page.keyboard.press("Enter")
        mark("submitted", box)

        page.wait_for_selector(".menu-opts button", timeout=180_000)
        time.sleep(0.4)
        mark("menu", page.locator(".card").first.bounding_box())
        time.sleep(1.6)
        btn = page.locator(".menu-opts button", has_text=pick).first
        b = btn.bounding_box()
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=30)
        time.sleep(0.3)
        btn.click()
        mark("picked", b)

        page.locator(".card").first.locator(".pill.running").wait_for(timeout=60_000)
        time.sleep(1.2)
        mark("working", page.locator(".card").first.bounding_box())
        card = page.locator(".card").first
        card.locator(".report iframe").wait_for(timeout=900_000)
        card.locator(".pill.done").wait_for(timeout=900_000)
        time.sleep(2.0)                                   # let the preview paint
        mark("report", None)
        rep = card.locator(".report").bounding_box()
        page.mouse.move(W * 0.72, rep["y"] + 60, steps=25)
        page.evaluate("y => window.scrollTo({top: y, behavior: 'smooth'})", rep["y"] - 90)
        time.sleep(1.6)
        rep = card.locator(".report").bounding_box()
        mark("read", rep)
        # scroll inside the report preview: the business overview → the statements
        frame = card.frame_locator(".report iframe")
        for y in (350, 900, 1500):
            frame.locator("body").evaluate("(b, y) => b.ownerDocument.defaultView.scrollTo({top: y, behavior: 'smooth'})", y)
            time.sleep(1.5)
        mark("end", None)
        time.sleep(0.8)
        video = Path(page.video.path())
        ctx.close()
        browser.close()
    srv.should_exit = True

    (workdir / "marks.json").write_text(json.dumps(marks, indent=1))
    t = {name: at for name, at, _ in marks}
    r = {name: rect for name, _, rect in marks}
    beats = [
        Beat(t["open"], t["type"], 1.0, None),                        # the page, once it has painted
        Beat(t["type"], t["submitted"] + 0.6, 1.0, r["type"]),        # typing — zoom on the box
        Beat(t["submitted"] + 0.6, t["menu"], 6.0, None),             # resolving the name (sped up)
        Beat(t["menu"], t["picked"] + 0.4, 1.0, r["menu"]),           # the pick buttons — zoom in
        Beat(t["picked"] + 0.4, t["working"], 1.0, r["working"]),     # the job card starts
        Beat(t["working"], t["report"], max(1.0, (t["report"] - t["working"]) / 3.0), r["working"]),  # ~5 min → 3 s
        Beat(t["report"], t["read"], 1.0, None),                      # scroll to the report
        Beat(t["read"], t["end"] + 0.8, 1.0, r["read"]),              # read it — zoom in
    ]
    return video, beats


def _ease(x: float) -> float:
    return x * x * (3 - 2 * x)


def _frame_rect(rect, pad: float = 0.12) -> tuple[float, float, float, float]:
    """Target crop for a region: padded, at the output aspect ratio, inside the frame, never
    zooming in further than 2× (text must stay readable)."""
    if rect is None:
        return (0.0, 0.0, float(W), float(H))
    x, y, w, h = rect
    w, h = w * (1 + 2 * pad), h * (1 + 2 * pad)
    aspect = W / H
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    w, h = max(w, W / 2), max(h, H / 2)                       # max 2× zoom
    w, h = min(w, W), min(h, H)
    cx, cy = x + rect[2] / 2, y + rect[3] / 2
    x0 = min(max(cx - w / 2, 0), W - w)
    y0 = min(max(cy - h / 2, 0), H - h)
    return (x0, y0, w, h)


def render(video: Path, beats: list[Beat], out_dir: Path, workdir: Path) -> tuple[Path, Path]:
    """Retime the recording per beat, then ease the camera between beats' regions → GIF + MP4."""
    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    # 1) retime: trim each beat, speed it, concat — at the final frame rate
    parts, filters = [], []
    for i, b in enumerate(beats):
        filters.append(f"[0:v]trim=start={b.t0:.3f}:end={b.t1:.3f},setpts=(PTS-STARTPTS)/{b.speed:.4f}[v{i}]")
        parts.append(f"[v{i}]")
    filt = ";".join(filters) + f";{''.join(parts)}concat=n={len(beats)}:v=1:a=0,fps={FPS}[out]"
    retimed = workdir / "retimed.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-filter_complex", filt,
                    "-map", "[out]", "-pix_fmt", "yuv420p", str(retimed)], check=True)
    frames_dir = workdir / "frames"
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir()
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(retimed),
                    str(frames_dir / "f%05d.png")], check=True)

    # 2) camera: each beat's output interval gets its region; ease over 0.7 s into the next one
    spans, t = [], 0.0
    for b in beats:
        dur = (b.t1 - b.t0) / b.speed
        spans.append((t, t + dur, _frame_rect(b.rect)))
        t += dur
    ease_s = 0.7

    def camera(ot: float) -> tuple[float, float, float, float]:
        for i, (a, z, rect) in enumerate(spans):
            if ot < z or i == len(spans) - 1:
                if i > 0 and ot < a + ease_s:
                    k = _ease(min(max((ot - a) / ease_s, 0.0), 1.0))
                    prev = spans[i - 1][2]
                    return tuple(p + (q - p) * k for p, q in zip(prev, rect))   # type: ignore[return-value]
                return rect
        return spans[-1][2]

    zoomed = workdir / "zoomed"
    shutil.rmtree(zoomed, ignore_errors=True)
    zoomed.mkdir()
    out_h = round(OUT_W * H / W / 2) * 2
    files = sorted(frames_dir.glob("f*.png"))
    for n, f in enumerate(files):
        x, y, w, h = camera(n / FPS)
        with Image.open(f) as im:
            im.crop((round(x), round(y), round(x + w), round(y + h))).resize(
                (OUT_W * 2, out_h * 2), Image.LANCZOS).save(zoomed / f.name)

    # 3) export: an MP4 (sharp, small) and a palette-optimised GIF for the README
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4, gif = out_dir / "demo.mp4", out_dir / "demo.gif"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i",
                    str(zoomed / "f%05d.png"), "-vf", f"scale={OUT_W * 2}:-2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "20", str(mp4)], check=True)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i",
                    str(zoomed / "f%05d.png"), "-vf",
                    f"scale={OUT_W}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                    "-loop", "0", str(gif)], check=True)
    return gif, mp4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="DuckDB to run against (default: EQR_DB_PATH / the repo's)")
    ap.add_argument("--outputs", help="where the server saves reports (default: a temp dir)")
    ap.add_argument("--query", default="adani")
    ap.add_argument("--pick", default="ADANIPOWER")
    ap.add_argument("--out", default=str(ROOT / "docs" / "media"))
    ap.add_argument("--rerender", help="re-render from a previous workdir (skip recording)")
    args = ap.parse_args()
    if args.db:
        os.environ["EQR_DB_PATH"] = args.db
    work = Path(args.rerender) if args.rerender else Path(tempfile.mkdtemp(prefix="eqr-gif-"))
    os.environ["EQR_OUTPUT_DIR"] = args.outputs or str(work / "outputs")
    os.environ["LITELLM_LOG"] = "ERROR"
    from equity_research.common.env import load_env
    load_env()

    if args.rerender:
        video = next(work.glob("*.webm"))
        beats = [Beat(**b) for b in json.loads((work / "beats.json").read_text())]
    else:
        video, beats = record(args.query, args.pick, work)
        (work / "beats.json").write_text(json.dumps([b.__dict__ for b in beats], indent=1))
    gif, mp4 = render(video, beats, Path(args.out), work)
    print(f"GIF {gif} ({gif.stat().st_size / 1e6:.1f} MB) · MP4 {mp4} ({mp4.stat().st_size / 1e6:.1f} MB)"
          f" · workdir {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
