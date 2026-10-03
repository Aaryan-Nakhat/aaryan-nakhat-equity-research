/* A tiny deterministic motion engine for the README videos: the page is a function of time.
   window.render(t) sets every animated element for time t (seconds), so frames can be captured one by one
   (scripts/render_video.py) — no real-time playback, no dropped frames.

   Markup:
     <section class="scene" data-t="4.5,13">          scene visible from 4.5 s to 13 s (fades in / out)
     <el data-in="0.6,0.8,up">                        enters 0.6 s after its scene starts, over 0.8 s
        types: fade · up · down · left · right · scale · pop · draw (SVG stroke) · grow (width %, data-to)
               · growy (height %, data-to) · count (data-to, data-dec, data-pre, data-suf) · type (data-text)
     <el data-out="6.5,0.5">                          leaves 6.5 s into its scene
     <el data-pulse="1.2,2.4">                        a dot travelling along its parent path, from 1.2 s, period 2.4 s
*/
(() => {
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);                       // easeOutCubic
  const back = (x) => { x = clamp(x); const c = 1.5; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
  const fmt = (v, dec) => v.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec });

  const scenes = [...document.querySelectorAll(".scene")].map((el) => {
    const [a, b] = el.dataset.t.split(",").map(Number);
    return { el, a, b };
  });
  document.querySelectorAll("[data-in]").forEach((el) => {
    const [d, dur, type] = el.dataset.in.split(",");
    el._in = { d: +d, dur: +dur, type: type.trim() };
    if (type.trim() === "draw") {
      const len = el.getTotalLength ? el.getTotalLength() : 1000;
      el.style.strokeDasharray = len;
      el._len = len;
    }
  });
  document.querySelectorAll("[data-out]").forEach((el) => {
    const [d, dur] = el.dataset.out.split(",").map(Number);
    el._out = { d, dur };
  });

  function sceneOf(el) {
    return scenes.find((s) => s.el.contains(el));
  }

  function apply(el, local) {
    let op = 1, tx = 0, ty = 0, sc = 1;
    if (el._in) {
      const { d, dur, type } = el._in;
      const p = ease((local - d) / dur);
      if (type === "fade") op = p;
      else if (type === "up") { op = p; ty = (1 - p) * 40; }
      else if (type === "down") { op = p; ty = -(1 - p) * 40; }
      else if (type === "left") { op = p; tx = (1 - p) * 60; }
      else if (type === "right") { op = p; tx = -(1 - p) * 60; }
      else if (type === "scale") { op = p; sc = 0.85 + 0.15 * p; }
      else if (type === "pop") { op = clamp((local - d) / (dur * 0.4)); sc = local < d ? 0.6 : 0.6 + 0.4 * back((local - d) / dur); }
      else if (type === "draw") { el.style.strokeDashoffset = el._len * (1 - p); op = local < d ? 0 : 1; }
      else if (type === "grow") { el.style.width = `${(+el.dataset.to) * p}%`; }
      else if (type === "growy") { el.style.height = `${(+el.dataset.to) * p}%`; }
      else if (type === "count") {
        const to = +el.dataset.to, dec = +(el.dataset.dec || 0);
        el.textContent = (el.dataset.pre || "") + fmt(to * p, dec) + (el.dataset.suf || "");
        op = local < d ? 0 : 1;
      } else if (type === "type") {
        const text = el.dataset.text;
        const n = Math.round(text.length * clamp((local - d) / dur));
        el.textContent = text.slice(0, n);
        el.classList.toggle("caret", local >= d - 0.4 && local < d + dur + 0.6);
      }
    }
    if (el._out) {
      const q = ease((local - el._out.d) / el._out.dur);
      op *= 1 - q;
      ty -= q * 20;
    }
    el.style.opacity = op;
    if (tx || ty || sc !== 1) el.style.transform = `translate(${tx}px, ${ty}px) scale(${sc})`;
    else el.style.transform = "";
  }

  function pulses(t) {
    document.querySelectorAll("[data-pulse]").forEach((dot) => {
      const sc = sceneOf(dot);
      const local = t - sc.a;
      const [start, period] = dot.dataset.pulse.split(",").map(Number);
      const path = document.getElementById(dot.dataset.path);
      if (!path || local < start) { dot.setAttribute("opacity", 0); return; }
      const f = ((local - start) % period) / period;
      const pt = path.getPointAtLength(path.getTotalLength() * ease(f));
      dot.setAttribute("cx", pt.x);
      dot.setAttribute("cy", pt.y);
      dot.setAttribute("opacity", f < 0.92 ? 1 : (1 - f) / 0.08);
    });
  }

  window.render = (t) => {
    for (const s of scenes) {
      const fadeIn = clamp((t - s.a) / 0.5), fadeOut = 1 - clamp((t - (s.b - 0.5)) / 0.5);
      const on = t >= s.a && t <= s.b;
      s.el.style.visibility = on ? "visible" : "hidden";        // (not display — scenes keep their own layout)
      s.el.style.opacity = Math.min(fadeIn, fadeOut);
      if (!on) continue;
      s.el.querySelectorAll("[data-in], [data-out]").forEach((el) => apply(el, t - s.a));
    }
    pulses(t);
    const bar = document.getElementById("progress");
    if (bar) bar.style.width = `${100 * t / window.DURATION}%`;
    const bg = document.getElementById("bg");
    if (bg) bg.style.transform = `translate(${-30 * Math.sin(t / 9)}px, ${-20 * Math.cos(t / 11)}px)`;
  };
  window.DURATION = Math.max(...scenes.map((s) => s.b));
  render(0);
})();
