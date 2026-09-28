/* Equity Research Workbench — web UI client. Plain JS: fetch + EventSource, no build step.
   Each command becomes a job card that follows the server's live event stream: notes (the "got
   it" acks), the saved report (previewed inline, with its PDFs), the numbered menu (as buttons —
   a click is the same in-thread "reply a number" the email bot understands), done / error. */
(() => {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const COMMANDS = window.EQR_COMMANDS || [];
  const feed = $("#feed"), empty = $("#empty"), q = $("#q"), suggest = $("#suggest");
  const cards = new Map();                      // job id → card state
  const REPORT_PREVIEW_PX = 520;

  // ── helpers ────────────────────────────────────────────────────────────────
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  /** Tiny, safe markdown for short notes: ```blocks```, **bold**, `code`, _italic_, line breaks. */
  function mdLite(text) {
    return String(text).split("```").map((part, i) => {
      if (i % 2) return `<pre>${esc(part.replace(/^\w*\n/, "").replace(/\n+$/, ""))}</pre>`;
      return esc(part.replace(/^\n+|\n+$/g, ""))       // no blank gaps around a code block
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/(^|[\s(])_(.+?)_(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>")
        .replace(/\n/g, "<br>");
    }).join("");
  }

  const clock = (secs) => `${Math.floor(secs / 60)}m ${String(Math.floor(secs % 60)).padStart(2, "0")}s`;
  const when = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // ── job cards ──────────────────────────────────────────────────────────────
  function newCard(title, sub) {
    empty.hidden = true;
    const el = $("#tpl-card").content.firstElementChild.cloneNode(true);
    $(".card-q", el).textContent = title;
    $(".card-sub", el).textContent = sub || "";
    feed.prepend(el);
    return el;
  }

  function addJobCard(job, title, sub) {
    const el = newCard(title, sub);
    const c = { el, job, lastSeq: 0, status: job.status, started: job.created * 1000, ended: null, timer: null };
    cards.set(job.id, c);
    setStatus(c, job.status);
    follow(c);
    return c;
  }

  function setStatus(c, status) {
    c.status = status;
    const pill = $(".pill", c.el);
    pill.className = `pill ${status}`;
    pill.textContent = { queued: "Queued", running: "Working", done: "Done", error: "Failed" }[status] || status;
    const out = $(".elapsed", c.el);
    clearInterval(c.timer);
    if (status === "queued" || status === "running") {
      const tick = () => { out.textContent = clock((Date.now() - c.started) / 1000); };
      tick();
      c.timer = setInterval(tick, 1000);
    } else if (c.ended) {
      out.textContent = clock((c.ended - c.started) / 1000);
    }
  }

  function follow(c) {
    const es = new EventSource(`/api/jobs/${c.job.id}/events?after=${c.lastSeq}`);
    es.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.seq <= c.lastSeq) return;
      c.lastSeq = ev.seq;
      handle(c, ev);
    };
    es.addEventListener("end", () => es.close());
    es.onerror = () => {                        // server restarted / network blip: resume where we were
      es.close();
      if (c.status !== "done" && c.status !== "error") setTimeout(() => follow(c), 3000);
    };
  }

  function handle(c, ev) {
    const body = $(".card-body", c.el);
    switch (ev.kind) {
      case "status":
        setStatus(c, ev.status);
        break;
      case "note": {
        const n = document.createElement("div");
        n.className = "note";
        n.innerHTML = mdLite(ev.text);
        body.append(n);
        break;
      }
      case "report":
        body.append(reportBlock(ev.title, ev.html_url, ev.attachments || []));
        break;
      case "menu":
        // the email-style text list ("1) HDFCBANK — …") is now buttons — don't show it twice
        body.querySelectorAll(".note pre").forEach((pre) => pre.replaceWith(document.createElement("br")));
        body.append(menuBlock(c, ev.options));
        break;
      case "done":
        c.ended = ev.ts * 1000;
        setStatus(c, "done");
        loadHistory();
        break;
      case "error": {
        c.ended = ev.ts * 1000;
        const e = document.createElement("div");
        e.className = "err";
        e.textContent = ev.message;
        body.append(e);
        setStatus(c, "error");
        break;
      }
    }
  }

  function reportBlock(title, url, attachments) {
    const wrap = document.createElement("div");
    wrap.className = "report collapsed";
    const pdfs = attachments.map((a) =>
      `<a href="${esc(a.url)}" download>⬇ ${esc(a.name.replace(/\.pdf$/i, "").replace(/_/g, " "))}</a>`).join("");
    wrap.innerHTML = `
      <div class="report-bar">
        <span class="report-title">📄 ${esc(title)}</span>
        <span class="report-actions"><a href="${esc(url)}" target="_blank" rel="noopener">Open full report ↗</a>${pdfs}</span>
      </div>
      <iframe loading="lazy" title="${esc(title)}" src="${esc(url)}"></iframe>
      <button type="button" class="report-more">Show the whole report ↓</button>`;
    const frame = $("iframe", wrap), more = $(".report-more", wrap);
    frame.addEventListener("load", () => {
      let h = 0;
      try { h = frame.contentDocument.documentElement.scrollHeight; } catch { /* cross-origin: keep preview */ }
      frame.dataset.full = h;
      if (h && h <= REPORT_PREVIEW_PX + 40) { frame.style.height = `${h}px`; more.hidden = true; }
      else frame.style.height = `${REPORT_PREVIEW_PX}px`;
    });
    more.addEventListener("click", () => {
      const open = wrap.classList.toggle("collapsed");
      frame.style.height = open ? `${REPORT_PREVIEW_PX}px` : `${Number(frame.dataset.full) + 24}px`;
      more.textContent = open ? "Show the whole report ↓" : "Collapse ↑";
    });
    return wrap;
  }

  function menuBlock(c, options) {
    const wrap = document.createElement("div");
    wrap.className = "menu";
    wrap.innerHTML = `<div class="menu-title">${options.length === 1 ? "Go deeper:" : "Pick one:"}</div><div class="menu-opts"></div>`;
    const box = $(".menu-opts", wrap);
    for (const o of options) {
      const b = document.createElement("button");
      b.type = "button";
      b.innerHTML = `<b>${o.n}</b><span>${esc(o.label)}</span>`;
      b.addEventListener("click", () => pick(c, o, b));
      box.append(b);
    }
    return wrap;
  }

  async function pick(c, option, button) {
    button.disabled = true;
    try {
      const job = await api("/api/jobs", { pick: option.n, root: c.job.root, subject: c.job.subject });
      addJobCard(job, option.label, `from “${c.job.subject}”`);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      button.disabled = false;
      showError(e.message);
    }
  }

  async function run(text) {
    text = text.trim();
    if (!text) return;
    hideSuggest();
    q.value = "";
    try {
      const job = await api("/api/jobs", { text });
      addJobCard(job, text);
    } catch (e) {
      showError(e.message);
    }
  }

  function showError(message) {
    const el = newCard("Couldn't start that", "");
    $(".pill", el).className = "pill error";
    $(".pill", el).textContent = "Error";
    $(".card-body", el).innerHTML = `<div class="err">${esc(message)}</div>`;
  }

  // ── history (saved reports, survives restarts) ─────────────────────────────
  async function loadHistory() {
    let items;
    try { items = await api("/api/history?limit=80"); } catch { return; }
    const list = $("#history");
    if (!items.length) return;
    list.innerHTML = "";
    let day = null;
    for (const it of items) {
      const d = it.ts.slice(0, 10);
      if (d !== day) {
        day = d;
        const h = document.createElement("li");
        h.className = "day";
        h.textContent = new Date(`${d}T00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
        list.append(h);
      }
      const li = document.createElement("li");
      li.innerHTML = `<a href="${esc(it.html)}">${esc(it.title)}<small>${esc(it.subject)} · ${when(it.ts)}</small></a>`;
      $("a", li).addEventListener("click", (e) => {
        e.preventDefault();
        const card = newCard(it.title, `saved ${new Date(it.ts).toLocaleString()}`);
        $(".pill", card).className = "pill done";
        $(".pill", card).textContent = "Saved";
        $(".card-body", card).append(reportBlock(it.title, it.html,
          (it.attachments || []).map((u) => ({ url: u, name: decodeURIComponent(u.split("__").pop()) }))));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
      list.append(li);
    }
  }

  // ── autocomplete over the real command list ────────────────────────────────
  let matches = [], active = -1;

  function renderSuggest() {
    const v = q.value.trim().toLowerCase();
    matches = v ? COMMANDS.filter((c) =>
      [c.cmd, ...c.also].some((f) => f.toLowerCase().includes(v)) || c.section.toLowerCase().includes(v)).slice(0, 8) : [];
    active = -1;
    if (!matches.length) return hideSuggest();
    suggest.innerHTML = matches.map((c, i) =>
      `<li role="option" data-i="${i}"><code>${esc(c.cmd)}</code><span>${esc(c.desc)}</span></li>`).join("");
    suggest.hidden = false;
  }
  function hideSuggest() { suggest.hidden = true; matches = []; active = -1; }
  function highlight(i) {
    active = i;
    [...suggest.children].forEach((li, k) => li.setAttribute("aria-selected", String(k === i)));
  }
  function choose(i) {
    const cmd = matches[i].cmd;
    const cut = cmd.indexOf("<");                 // e.g. "sector: <name>" → type the name next
    hideSuggest();
    if (cut >= 0) { q.value = cmd.slice(0, cut); q.focus(); } else run(cmd);
  }

  q.addEventListener("input", renderSuggest);
  q.addEventListener("keydown", (e) => {
    if (suggest.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight((active + 1) % matches.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight((active - 1 + matches.length) % matches.length); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(active); }
    else if (e.key === "Escape") hideSuggest();
  });
  suggest.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (li) { e.preventDefault(); choose(Number(li.dataset.i)); }
  });
  q.addEventListener("blur", () => setTimeout(hideSuggest, 120));

  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); run(q.value); });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-cmd]");
    if (b) run(b.dataset.cmd);
  });
  $("#newBtn").addEventListener("click", () => { window.scrollTo({ top: 0, behavior: "smooth" }); q.focus(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== q) { e.preventDefault(); q.focus(); }
  });

  // ── start: re-attach to jobs still in memory on the server, load history ────
  (async () => {
    try {
      const jobs = await api("/api/jobs");
      for (const j of jobs.reverse()) {           // oldest first, so the newest ends on top
        const isPick = j.text.startsWith("pick ");
        addJobCard(j, isPick ? `${j.subject} → ${j.text}` : j.text, isPick ? "a numbered pick" : "");
      }
    } catch { /* server just started */ }
    loadHistory();
  })();
})();
