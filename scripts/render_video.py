"""Render a docs/media/src/*.html motion page (see motion.js) to MP4 + GIF, frame by frame.

    uv run python scripts/render_video.py docs/media/src/hero.html --out docs/media/hero
    uv run python scripts/render_video.py docs/media/src/hero.html --stills 3,9,17,25,33,40,46   # preview PNGs

Each frame is the page at exactly t = i / fps (``window.render(t)``), screenshotted in headless Chromium and
piped to ffmpeg — deterministic, no dropped frames. Needs ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

W, H = 1600, 900


def _page(pw, src: Path):
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    page.goto(src.resolve().as_uri())
    page.wait_for_function("document.fonts.status === 'loaded' && typeof window.render === 'function'")
    page.wait_for_timeout(300)
    return browser, page


def stills(src: Path, times: list[float], out_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser, page = _page(pw, src)
        for t in times:
            page.evaluate(f"render({t})")
            page.screenshot(path=str(out_dir / f"{src.stem}-{t:05.1f}s.png"))
        browser.close()


def video(src: Path, out: Path, fps: int, gif_width: int, gif_fps: int) -> None:
    from playwright.sync_api import sync_playwright

    ffmpeg = shutil.which("ffmpeg") or sys.exit("ffmpeg not found on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    mp4 = out.with_suffix(".mp4")
    enc = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                            "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "slow",
                            "-movflags", "+faststart", str(mp4)], stdin=subprocess.PIPE)
    with sync_playwright() as pw:
        browser, page = _page(pw, src)
        duration = page.evaluate("window.DURATION")
        n = int(duration * fps)
        for i in range(n):
            page.evaluate(f"render({i / fps})")
            enc.stdin.write(page.screenshot(type="png"))
            if i % (fps * 5) == 0:
                print(f"  {i / fps:5.1f}s / {duration:.1f}s", flush=True)
        browser.close()
    enc.stdin.close()
    enc.wait()
    gif = out.with_suffix(".gif")
    vf = f"fps={gif_fps},scale={gif_width}:-1:flags=lanczos"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(mp4), "-vf",
                    f"{vf},split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"     # ~7 MB for 48 s at 720 px
                    f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", str(gif)], check=True)
    for f in (mp4, gif):
        print(f"wrote {f} ({f.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, help="output path without extension (writes .mp4 and .gif)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--gif-width", type=int, default=720)
    ap.add_argument("--gif-fps", type=int, default=10)
    ap.add_argument("--stills", help="comma-separated times (s) → PNG previews instead of a video")
    ap.add_argument("--stills-dir", type=Path, default=Path("data/outputs/media-preview"))
    a = ap.parse_args()
    if a.stills:
        stills(a.src, [float(x) for x in a.stills.split(",")], a.stills_dir)
    else:
        video(a.src, a.out or Path("docs/media") / a.src.stem, a.fps, a.gif_width, a.gif_fps)


if __name__ == "__main__":
    main()
