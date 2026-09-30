/* 💼 My holdings — your lots (stock · qty · buy price · optional date), valued on the latest close.
   Lots typed here can be edited / deleted; lots from holdings.csv are edited in that file. Everything
   stays on this machine. */
(() => {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const panel = $("#holdings"), feed = $("#feed"), empty = $("#empty");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const inr = (n, d = 0) => n == null ? "—" : (n < 0 ? "−₹" : "₹") + Math.abs(Number(n)).toLocaleString("en-IN",
    { minimumFractionDigits: d, maximumFractionDigits: d });
  const num = (n, d = 2) => n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d });
  const pct = (n) => n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
  const tone = (n) => n == null ? "" : n >= 0 ? "up" : "down";
  const fmtDate = (iso) => iso ? new Date(`${iso}T00:00`).toLocaleDateString("en-IN",
    { day: "2-digit", month: "short", year: "numeric" }) : "";

  async function call(method, path, body) {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    if (!res.ok) {
      const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // ── show / hide ─────────────────────────────────────────────────────────────
  function open() {
    panel.hidden = false; feed.hidden = true; empty.hidden = true;
    document.body.classList.add("on-holdings");
    load();
  }
  function close() {
    if (panel.hidden) return;
    panel.hidden = true; feed.hidden = false;
    empty.hidden = feed.children.length > 0;
    document.body.classList.remove("on-holdings");
  }
  $("#holdingsBtn").addEventListener("click", open);
  // any command / new question goes back to the research feed
  $("#ask").addEventListener("submit", close, true);
  $("#newBtn").addEventListener("click", close, true);
  document.addEventListener("click", (e) => { if (e.target.closest("[data-cmd]")) close(); }, true);

  // ── render ──────────────────────────────────────────────────────────────────
  function lotDetail(l) {
    const bits = [];
    if (l.buy_date) {
      bits.push(l.term === "long" ? `<span class="tag ok">long-term</span>`
        : `<span class="tag warn">short-term · long-term in ${l.days_to_long}d</span>`);
      if (l.yearly_pct != null) bits.push(`${pct(l.yearly_pct)} a year`);
      if (l.bench_pct != null) bits.push(`Nifty 500 ${pct(l.bench_pct)} since`);
    } else bits.push(`<span class="muted">no date — P&amp;L only</span>`);
    if (l.adjusted && l.adjusted.length)
      bits.push(`<span class="muted" title="${esc(l.adjusted.join("; "))}">adjusted for ${l.adjusted.length} split/bonus</span>`);
    return bits.join(" · ");
  }

  function lotRow(l) {
    const tr = document.createElement("tr");
    tr.className = "lot";
    tr.dataset.id = l.id;
    const csv = l.source === "csv";
    tr.innerHTML = `
      <td class="indent">${l.buy_date ? fmtDate(l.buy_date) : "<span class='muted'>undated</span>"}
        ${csv ? "<span class='tag'>csv</span>" : ""}</td>
      <td class="n">${num(l.adj_qty, 4)}</td>
      <td class="n">${inr(l.adj_price, 2)}</td>
      <td class="n">${inr(l.value)}</td>
      <td class="n ${tone(l.pnl)}">${inr(l.pnl)} <small>${pct(l.pnl_pct)}</small></td>
      <td class="detail">${lotDetail(l)}</td>
      <td class="acts">${csv ? "<span class='muted small' title='Edit holdings.csv to change this lot'>in file</span>"
        : `<button type="button" data-act="edit" title="Edit">✎</button><button type="button" data-act="del" title="Delete">🗑</button>`}</td>`;
    tr.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      if (b.dataset.act === "del") remove(l, b);
      else edit(tr, l);
    });
    return tr;
  }

  function missRow(m) {
    const tr = document.createElement("tr");
    tr.className = "miss";
    tr.innerHTML = `
      <td><b>${esc(m.name)}</b> <small class="muted">${esc(m.symbol)}</small></td>
      <td class="n"><input type="number" step="any" min="0" placeholder="Qty" aria-label="Quantity"></td>
      <td class="n"><input type="number" step="any" min="0" placeholder="₹ paid" aria-label="Buy price"></td>
      <td colspan="3"><input type="date" max="${new Date().toISOString().slice(0, 10)}" aria-label="Buy date (optional)"
        title="Buy date (optional)"></td>
      <td class="acts"><button type="button" class="save">Save</button></td>`;
    const [q, p, d] = tr.querySelectorAll("input");
    const save = async () => {
      if (!q.value || !p.value) { flash(`Enter the quantity and the price you paid for ${m.name}.`); return; }
      try {
        await call("POST", "/api/holdings", { stock: m.symbol, qty: q.value, price: p.value, date: d.value || null });
        flash(`Saved ${m.name}`, true);
        load();
      } catch (err) { flash(err.message); }
    };
    $(".save", tr).addEventListener("click", save);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    return tr;
  }

  function edit(tr, l) {
    tr.innerHTML = `
      <td class="indent"><input type="date" value="${esc(l.buy_date || "")}" max="${new Date().toISOString().slice(0, 10)}"></td>
      <td class="n"><input type="number" step="any" min="0" value="${esc(l.qty)}"></td>
      <td class="n"><input type="number" step="any" min="0" value="${esc(l.price)}"></td>
      <td colspan="3" class="muted small">Enter qty and price as you bought them — splits / bonuses after the date are applied for you.</td>
      <td class="acts"><button type="button" class="save">Save</button><button type="button" class="cancel">✕</button></td>`;
    const [d, q, p] = tr.querySelectorAll("input");
    $(".cancel", tr).addEventListener("click", load);
    $(".save", tr).addEventListener("click", async () => {
      try { await call("PUT", `/api/holdings/${l.id}`, { qty: q.value, price: p.value, date: d.value || null }); load(); }
      catch (err) { flash(err.message); }
    });
  }

  async function remove(l, button) {
    if (!confirm(`Delete this ${l.symbol} lot (${num(l.qty, 4)} @ ${inr(l.price, 2)})?`)) return;
    button.disabled = true;
    try { await call("DELETE", `/api/holdings/${l.id}`); load(); } catch (err) { flash(err.message); button.disabled = false; }
  }

  function flash(msg, ok = false) {
    const f = $("#hFlash");
    f.className = ok ? "flash ok" : "flash err";
    f.textContent = msg;
    f.hidden = false;
    clearTimeout(flash.t);
    flash.t = setTimeout(() => { f.hidden = true; }, ok ? 3000 : 8000);
  }

  function render(data) {
    const t = data.total;
    $("#hSummary").innerHTML = data.stocks.length ? `
      <div><small>Invested</small><b>${inr(t.cost)}</b></div>
      <div><small>Value now</small><b>${inr(t.value)}</b></div>
      <div class="${tone(t.pnl)}"><small>Profit / loss</small><b>${inr(t.pnl)} <em>${pct(t.pnl_pct)}</em></b></div>
      <div><small>Stocks · lots</small><b>${t.n_stocks} · ${t.n_lots}</b></div>` : "";
    const body = $("#hRows");
    body.innerHTML = "";
    const miss = data.missing || [];
    if (miss.length) {
      const head = document.createElement("tr");
      head.className = "miss-head";
      head.innerHTML = `<td colspan="7">📝 <b>${miss.length} in your watchlist without numbers yet</b> — type how many
        you hold and the price you paid (the date is optional; with it you also get tax and yearly return).</td>`;
      body.append(head);
      for (const m of miss) body.append(missRow(m));
    }
    if (!data.stocks.length && !miss.length) {
      body.innerHTML = `<tr><td colspan="7" class="muted blank">No holdings yet — add a buy above, or put a
        <code>holdings.csv</code> in the project folder.</td></tr>`;
    }
    for (const s of data.stocks) {
      const tr = document.createElement("tr");
      tr.className = "stock";
      tr.innerHTML = `
        <td><b>${esc(s.name)}</b> <small class="muted">${esc(s.symbol)}</small></td>
        <td class="n">${num(s.qty, 4)}</td>
        <td class="n">${inr(s.avg_price, 2)}</td>
        <td class="n">${s.priced ? inr(s.value) : "<span class='muted'>no price</span>"}</td>
        <td class="n ${tone(s.pnl)}">${s.priced ? `${inr(s.pnl)} <small>${pct(s.pnl_pct)}</small>` : "—"}</td>
        <td class="detail muted">${s.ltp != null ? `last close ${inr(s.ltp, 2)}` : ""}${s.weight_pct != null ? ` · ${s.weight_pct.toFixed(1)}% of portfolio` : ""}</td>
        <td></td>`;
      body.append(tr);
      for (const l of s.lots) body.append(lotRow(l));
    }
    const c = data.csv || {};
    const csvBox = $("#hCsv");
    if (c.status === "none") {
      csvBox.innerHTML = `Have many stocks? Put them in <code>${esc(c.path)}</code> — columns <code>symbol, qty, price, date</code>
        (date optional; your broker's holdings export works too). It's read automatically whenever it changes.`;
    } else {
      csvBox.innerHTML = `📄 <code>${esc(c.path)}</code> — ${c.status === "imported" ? `just imported ${c.imported} lot(s)` : "in sync"}.`
        + (c.errors && c.errors.length ? `<ul class="csv-err">${c.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "");
    }
  }

  async function load() {
    try { render(await call("GET", "/api/holdings")); }
    catch (err) { flash(`Couldn't load holdings: ${err.message}`); }
  }

  // company-name autocomplete: type any part of the name, pick from the list
  const form = $("#hForm"), box = $("#hSuggest");
  let picked = null, hits = [], active = -1, seq = 0;
  const hideBox = () => { box.hidden = true; active = -1; };
  function pickHit(i) {
    picked = hits[i];
    form.stock.value = picked.name;
    hideBox();
    form.qty.focus();
  }
  form.stock.addEventListener("input", async () => {
    picked = null;
    const v = form.stock.value.trim();
    if (v.length < 2) return hideBox();
    const mine = ++seq;
    let res = [];
    try { res = await call("GET", `/api/stocks?q=${encodeURIComponent(v)}`); } catch { return; }
    if (mine !== seq) return;                    // a newer keystroke already asked
    hits = res;
    if (!hits.length) {
      box.innerHTML = `<li class="muted">No company matches “${esc(v)}”</li>`;
      box.hidden = false;
      return;
    }
    box.innerHTML = hits.map((h, i) => `<li role="option" data-i="${i}"><b>${esc(h.name)}</b><span>${esc(h.symbol)}</span></li>`).join("");
    box.hidden = false;
    active = -1;
  });
  form.stock.addEventListener("keydown", (e) => {
    if (box.hidden || !hits.length) return;
    const lis = [...box.children];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
      lis.forEach((li, k) => li.setAttribute("aria-selected", String(k === active)));
    } else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pickHit(active); }
    else if (e.key === "Escape") hideBox();
  });
  box.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); pickHit(Number(li.dataset.i)); }
  });
  form.stock.addEventListener("blur", () => setTimeout(hideBox, 150));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = $("button[type=submit]", f);
    if (!picked && hits.length === 1) picked = hits[0];
    btn.disabled = true;
    try {
      const lot = await call("POST", "/api/holdings", { stock: picked ? picked.symbol : f.stock.value, qty: f.qty.value,
        price: f.price.value, date: f.date.value || null });
      flash(`Added ${lot.name} — ${num(lot.qty, 4)} @ ${inr(lot.price, 2)}`, true);
      picked = null; hits = [];
      f.reset();
      f.stock.focus();
      load();
    } catch (err) { flash(err.message); }
    btn.disabled = false;
  });
  $("#hForm").date.max = new Date().toISOString().slice(0, 10);

  // "Need cash?" → the same `raise <amount>` command, run as a job in the research feed
  $("#hRaise").addEventListener("submit", (e) => {
    e.preventDefault();
    const amt = e.target.amount.value.trim();
    if (!amt) return;
    $("#q").value = `raise ${amt}`;
    e.target.reset();
    $("#ask").requestSubmit();
  });
})();
