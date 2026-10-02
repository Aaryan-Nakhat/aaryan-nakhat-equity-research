/* 💼 My holdings — your buys and sells, valued on the latest close, with tax lots, realised gains by year,
   dividends and XIRR (all worked out server-side, portfolio/). Entries typed here can be edited / deleted;
   entries from holdings.csv are changed in that file. Everything stays on this machine. */
(() => {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const panel = $("#holdings"), feed = $("#feed"), empty = $("#empty");
  const TODAY = new Date().toISOString().slice(0, 10);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const inr = (n, d = 0) => n == null ? "—" : (n < 0 ? "−₹" : "₹") + Math.abs(Number(n)).toLocaleString("en-IN",
    { minimumFractionDigits: d, maximumFractionDigits: d });
  const num = (n, d = 2) => n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d });
  const pct = (n) => n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
  const tone = (n) => n == null ? "" : n >= 0 ? "up" : "down";
  const fmtDate = (iso) => iso ? new Date(`${iso}T00:00`).toLocaleDateString("en-IN",
    { day: "2-digit", month: "short", year: "numeric" }) : "";
  const KIND = { etf: "ETF", sme: "SME", reit: "REIT", invit: "InvIT", bse: "BSE only", former: "merged / delisted" };

  async function call(method, path, body) {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    if (!res.ok) {
      const detail = await res.json().then((j) => j.detail).catch(() => res.statusText);
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  function flash(msg, ok = false) {
    const f = $("#hFlash");
    f.className = ok ? "flash ok" : "flash err";
    f.textContent = msg;
    f.hidden = false;
    clearTimeout(flash.t);
    flash.t = setTimeout(() => { f.hidden = true; }, ok ? 3000 : 8000);
  }

  // ── show / hide ─────────────────────────────────────────────────────────────
  function open() {
    panel.hidden = false; feed.hidden = true; empty.hidden = true;
    load();
  }
  function close() {
    if (panel.hidden) return;
    panel.hidden = true; feed.hidden = false;
    empty.hidden = feed.children.length > 0;
  }
  $("#holdingsBtn").addEventListener("click", open);
  $("#ask").addEventListener("submit", close, true);             // any command goes back to the research feed
  $("#newBtn").addEventListener("click", close, true);
  document.addEventListener("click", (e) => { if (e.target.closest("[data-cmd]")) close(); }, true);

  // ── one buy's details ───────────────────────────────────────────────────────
  function lotDetail(l) {
    const bits = [];
    if (l.buy_date) {
      bits.push(l.term === "long" ? `<span class="tag ok">long-term</span>`
        : `<span class="tag warn">short-term · long-term in ${l.days_to_long}d</span>`);
      if (l.yearly_pct != null) bits.push(`${pct(l.yearly_pct)} a year`);
      if (l.bench_pct != null) bits.push(`Nifty 500 ${pct(l.bench_pct)} since`);
    } else bits.push(`<span class="muted">no date — P&amp;L only</span>`);
    if (l.from) bits.push(`<span class="muted">✂️ from your <b>${esc(l.from.name)}</b> buy — demerger ${fmtDate(l.from.date)},
      ${l.from.pct.toFixed(2)}% of its cost${l.from.url ? ` (<a href="${esc(l.from.url)}" target="_blank" rel="noopener">notice ↗</a>)` : ""};
      updates by itself when you change that buy</span>`);
    else if (l.received && !l.duplicate) bits.push(`<span class="muted">received ${fmtDate(l.received)} (demerger)</span>`);
    if (l.via) bits.push(`<span class="muted">you entered ${num(l.qty, 4)} <b>${esc(l.via.name)}</b> @ ${inr(l.price, 2)} →
      ${num(l.via.shares, 4)} shares here (merged ${fmtDate(l.via.date)}, ${esc(l.via.ratio)}${l.via.url
        ? `, <a href="${esc(l.via.url)}" target="_blank" rel="noopener">filing ↗</a>` : ""})</span>`);
    if (l.note) bits.push(`<span class="lot-note">🔁 ${esc(l.note)}</span>`);
    if (l.adjusted && l.adjusted.length && !l.via)
      bits.push(`<span class="muted">you entered ${num(l.qty, 4)} @ ${inr(l.price, 2)} — then ${esc(l.adjusted.join("; "))}</span>`);
    if (l.dividends) bits.push(`💰 dividends ${inr(l.dividends)}`);
    if (l.sold) bits.push(`<span class="muted">${num(l.sold, 4)} sold</span>`);
    if (l.grandfathered) bits.push(`<span class="muted" title="Held since 31-Jan-2018: that day's price counts as cost for tax">grandfathered</span>`);
    let out = bits.join(" · ");
    const bonus = (l.parts || []).filter((p) => p.kind === "bonus");
    if (bonus.length)
      out += `<div class="lot-sub">🎁 ${bonus.map((p) => `${num(p.shares, 4)} bonus shares since ${fmtDate(p.acquired)}`).join(", ")}
        — ₹0 cost and their own date for tax (${bonus.every((p) => p.term === "long") ? "long-term" : "short-term for now"}).</div>`;
    for (const n of l.events || []) out += `<div class="lot-warn">ℹ️ ${esc(n)}</div>`;
    for (const r of l.rights || []) out += rightsNote(l, r);
    for (const dm of l.demergers || []) out += demergerNote(l, dm);
    if (l.warn) out += `<div class="lot-warn">⚠️ ${esc(l.warn)}</div>`;
    return out;
  }

  function rightsNote(l, r) {
    const price = r.price != null ? ` @ ${inr(r.price, 2)}` : "";
    const what = r.entitled != null ? `you could apply for <b>${num(r.entitled, 4)}</b> shares${price}`
      : "the record doesn't state the ratio";
    return `<div class="lot-sub">🎟️ <b>Rights issue ${fmtDate(r.date)}</b> (${esc(r.ratio)}): ${what}. Did you subscribe?
      <button type="button" class="add-rights"
      data-sym="${esc(l.symbol)}" data-qty="${r.entitled ?? ""}" data-price="${r.price ?? ""}" data-date="${esc(r.date)}">
      ＋ I subscribed — add them</button> <span class="muted">(change the date to your allotment date if you know it)</span></div>`;
  }

  function demergerNote(l, dm) {
    const when = fmtDate(dm.ex_date);
    if (dm.basis === "filing") {
      const src = dm.url ? ` (<a href="${esc(dm.url)}" target="_blank" rel="noopener">company's notice ↗</a>)` : "";
      let html = `✂️ <b>Demerger ${when}:</b> ${dm.parent_pct.toFixed(2)}% of your cost stays here${src}.`;
      for (const c of dm.children) {
        const what = c.qty != null ? `<b>${num(c.qty, 4)}</b> shares of <b>${esc(c.name)}</b>` : `shares of <b>${esc(c.name)}</b>`;
        html += c.carried
          ? ` ${what} (${c.pct.toFixed(2)}% of your cost, ${inr(c.cost)}, same buy date) — <span class="tag ok">carried over automatically</span>`
          : ` You should also have ${what} — ${c.pct.toFixed(2)}% of your cost (${inr(c.cost)}), same buy date; it isn't
            listed (or the ratio isn't stated), so it isn't tracked here.`;
      }
      return `<div class="lot-dm">${html}</div>`;
    }
    if (dm.basis === "looking")
      return `<div class="lot-dm">✂️ <b>Demerger ${when}:</b> reading the company's cost-split notice…
        ${dm.parent_pct != null ? `(for now a market-price estimate: ~${dm.parent_pct.toFixed(0)}% of your cost kept here)` : ""}</div>`;
    if (dm.basis === "estimate")
      return `<div class="lot-dm">✂️ <b>Demerger ${when}:</b> ~${dm.parent_pct.toFixed(0)}% of your cost kept here — a
        <b>market-price estimate</b>; the company's cost-split notice wasn't found. Add the new company's shares yourself
        (same buy date).</div>`;
    return `<div class="lot-dm">✂️ <b>Demerger ${when}:</b> the cost split isn't known — your cost here isn't split. Add the new
      company's shares yourself (same buy date) and check the company's notice.</div>`;
  }

  // ── rows ────────────────────────────────────────────────────────────────────
  function lotRow(l) {
    const tr = document.createElement("tr");
    tr.className = "lot";
    const csv = l.source === "csv", auto = l.source === "auto";
    if (l.duplicate) tr.classList.add("dupe");
    tr.innerHTML = `
      <td class="indent">${l.buy_date ? fmtDate(l.buy_date) : "<span class='muted'>undated</span>"}
        ${csv ? "<span class='tag'>csv</span>" : ""}${auto ? "<span class='tag'>auto</span>" : ""}</td>
      <td class="n">${num(l.adj_qty, 4)}</td>
      <td class="n">${inr(l.adj_price, 2)}</td>
      <td class="n">${inr(l.value)}</td>
      <td class="n ${tone(l.pnl)}">${inr(l.pnl)} <small>${pct(l.pnl_pct)}</small></td>
      <td class="detail">${lotDetail(l)}</td>
      <td class="acts">${csv ? "<span class='muted small' title='Change it in holdings.csv'>in file</span>"
        : auto ? "" : l.duplicate ? `<button type="button" data-act="del" title="Delete">🗑</button>`
        : `<button type="button" data-act="edit" title="Edit">✎</button><button type="button" data-act="del" title="Delete">🗑</button>`}</td>`;
    tr.addEventListener("click", async (e) => {
      const add = e.target.closest("button.add-rights");
      if (add) {
        if (!add.dataset.price) {
          anotherBuy({ symbol: add.dataset.sym, name: l.name }, { qty: add.dataset.qty, date: add.dataset.date });
          flash("Enter the rights shares you got and the price you paid, then Add.");
          return;
        }
        add.disabled = true;
        try {
          await call("POST", "/api/holdings", { stock: add.dataset.sym, qty: add.dataset.qty, price: add.dataset.price,
            date: add.dataset.date || null });
          flash("Added your rights shares", true);
          load();
        } catch (err) { flash(err.message); add.disabled = false; }
        return;
      }
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
      <td colspan="3"><input type="date" max="${TODAY}" aria-label="Buy date (optional)" title="Buy date (optional)"></td>
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
      <td class="indent"><input type="date" value="${esc(l.buy_date || "")}" max="${TODAY}" title="Buy date"></td>
      <td class="n"><input type="number" step="any" min="0" value="${esc(l.qty)}"></td>
      <td class="n"><input type="number" step="any" min="0" value="${esc(l.price)}"></td>
      <td colspan="3" class="muted small">With a date: qty and price <b>as you bought them</b> (splits / bonuses since are applied).
        No date: what your broker shows today.${l.via ? ` These are the <b>${esc(l.via.name)}</b> shares you bought.` : ""}</td>
      <td class="acts"><button type="button" class="save">Save</button><button type="button" class="cancel">✕</button></td>`;
    const [d, q, p] = tr.querySelectorAll("input");
    $(".cancel", tr).addEventListener("click", load);
    $(".save", tr).addEventListener("click", async () => {
      try {
        await call("PUT", `/api/holdings/${l.id}`, { qty: q.value, price: p.value, date: d.value || null,
          received: l.received || null });
        load();
      } catch (err) { flash(err.message); }
    });
  }

  async function remove(l, button) {
    if (!confirm(`Delete this ${l.name} buy (${num(l.qty, 4)} @ ${inr(l.price, 2)})?`)) return;
    button.disabled = true;
    try { await call("DELETE", `/api/holdings/${l.id}`); load(); } catch (err) { flash(err.message); button.disabled = false; }
  }

  // "Sell" on a stock's line → a small form row under it
  function sellRow(s) {
    const tr = document.createElement("tr");
    tr.className = "sell-form";
    tr.innerHTML = `
      <td class="indent"><input type="date" max="${TODAY}" required aria-label="Sell date" title="Sell date"></td>
      <td class="n"><input type="number" step="any" min="0" placeholder="Qty sold" aria-label="Quantity sold"></td>
      <td class="n"><input type="number" step="any" min="0" placeholder="₹ per share" aria-label="Sell price"></td>
      <td colspan="3" class="small"><label><input type="checkbox"> tendered in a <b>buyback</b></label>
        <span class="muted">— as on your contract note; your oldest shares go first (FIFO).</span></td>
      <td class="acts"><button type="button" class="save">Save sell</button><button type="button" class="cancel">✕</button></td>`;
    const [d, q, p, bb] = tr.querySelectorAll("input");
    $(".cancel", tr).addEventListener("click", () => tr.remove());
    $(".save", tr).addEventListener("click", async () => {
      if (!d.value || !q.value || !p.value) { flash("Enter the sell date, quantity and price."); return; }
      try {
        await call("POST", "/api/sells", { stock: s.symbol, qty: q.value, price: p.value, date: d.value,
          kind: bb.checked ? "buyback" : "sell" });
        flash(`Recorded the sell of ${s.name}`, true);
        load();
      } catch (err) { flash(err.message); }
    });
    return tr;
  }

  function stockRow(s, body) {
    const tr = document.createElement("tr");
    tr.className = "stock";
    const tag = KIND[s.kind] ? ` <span class="tag">${KIND[s.kind]}</span>` : "";
    const extras = [];
    if (s.ltp != null) extras.push(`last close ${inr(s.ltp, 2)}`);
    if (s.weight_pct != null) extras.push(`${s.weight_pct.toFixed(1)}% of portfolio`);
    if (s.dividends) extras.push(`dividends ${inr(s.dividends)}`);
    if (s.realised_gain) extras.push(`booked ${inr(s.realised_gain)}`);
    if (s.xirr_pct != null) extras.push(`XIRR ${pct(s.xirr_pct)}`);
    tr.innerHTML = `
      <td><b>${esc(s.name)}</b> <small class="muted">${esc(s.symbol)}</small>${tag}</td>
      <td class="n">${num(s.qty, 4)}</td>
      <td class="n">${inr(s.avg_price, 2)}</td>
      <td class="n">${s.priced ? inr(s.value) : "<span class='muted'>no price</span>"}</td>
      <td class="n ${tone(s.pnl)}">${s.priced ? `${inr(s.pnl)} <small>${pct(s.pnl_pct)}</small>` : "—"}</td>
      <td class="detail muted">${extras.join(" · ")}</td>
      <td class="acts">${s.kind === "former" ? "" : `<button type="button" class="another" title="Add another buy of this stock">＋ Buy</button><button type="button" class="sell" title="Record a sell">Sell</button>`}</td>`;
    if (s.kind !== "former") {
      $(".another", tr).addEventListener("click", () => anotherBuy(s));
      $(".sell", tr).addEventListener("click", () => {
        if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("sell-form")) return;
        tr.after(sellRow(s));
        $("input", tr.nextElementSibling).focus();
      });
    }
    body.append(tr);
  }

  // ── realised gains by financial year ────────────────────────────────────────
  function renderRealised(data) {
    const box = $("#hRealised");
    const years = data.realised || [];
    if (!years.length) { box.innerHTML = ""; return; }
    const ui = new Set((data.sells || []).filter((s) => s.source === "ui").map((s) => s.id));
    box.innerHTML = `<h2>📒 Booked — realised gains by year</h2>` + years.map((y, i) => {
      const t = y.tax;
      const totals = [`short-term ${inr(t.st_gain)}`, `long-term ${inr(t.lt_gain)}`
        + (t.exemption_used ? ` (${inr(t.exemption_used)} tax-free)` : ""), `est. tax <b>${inr(t.tax)}</b>`];
      if (y.dividends) totals.push(`dividends received ${inr(y.dividends)} (taxed at your slab)`);
      if (y.deemed_dividend) totals.push(`buyback proceeds taxed as dividend ${inr(y.deemed_dividend)}`);
      if (y.exempt_buyback) totals.push(`exempt buyback gain ${inr(y.exempt_buyback)}`);
      const rows = y.rows.map((r) => `<tr>
          <td>${fmtDate(r.date)}</td><td>${esc(r.name)}</td><td class="n">${num(r.shares, 4)}</td>
          <td class="n">${inr(r.proceeds)}</td><td class="n">${r.cost == null ? "—" : inr(r.cost)}</td>
          <td class="n ${tone(r.gain)}">${r.gain == null ? "—" : inr(r.gain)}</td>
          <td>${r.treatment === "unmatched" ? "<span class='tag warn'>no matching buy</span>"
            : `<span class="tag ${r.term === "long" ? "ok" : ""}">${r.term}</span>`}
            ${r.kind === "buyback" ? ` <span class="tag">buyback · ${esc(r.treatment.replace("_", " "))}</span>` : ""}
            ${r.grandfathered ? " <span class='muted small'>grandfathered</span>" : ""}
            ${r.acquired ? `<span class="muted small"> bought ${fmtDate(r.acquired)}</span>` : ""}</td>
          <td class="acts">${ui.has(r.sell_id) ? `<button type="button" data-sell="${esc(r.sell_id)}" title="Delete this sell">🗑</button>` : ""}</td>
        </tr>`).join("");
      return `<details class="h-year" ${i === 0 ? "open" : ""}><summary><b>${esc(y.fy)}</b> — ${totals.join(" · ")}</summary>
        <table class="h-table small"><thead><tr><th>Sold</th><th>Stock</th><th class="n">Shares</th><th class="n">Got</th>
        <th class="n">Cost (tax)</th><th class="n">Gain</th><th>Term</th><th></th></tr></thead><tbody>${rows}</tbody></table></details>`;
    }).join("") + `<p class="muted small">An estimate for planning (FIFO, set-off, the yearly long-term exemption, 31-Jan-2018
      grandfathering) — not tax advice. Only sells you record here are counted.</p>`;
    box.querySelectorAll("button[data-sell]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Delete this sell?")) return;
      try { await call("DELETE", `/api/sells/${b.dataset.sell}`); load(); } catch (err) { flash(err.message); }
    }));
  }

  function render(data) {
    const t = data.total;
    const cards = data.stocks.length ? [
      `<div><small>Invested</small><b>${inr(t.cost)}</b></div>`,
      `<div><small>Value now</small><b>${inr(t.value)}</b></div>`,
      `<div class="${tone(t.pnl)}"><small>Profit / loss</small><b>${inr(t.pnl)} <em>${pct(t.pnl_pct)}</em></b></div>`,
      `<div><small>Dividends received</small><b>${inr(t.dividends)}</b></div>`,
      `<div class="${tone(t.xirr_pct)}"><small>XIRR (dated buys)</small><b>${t.xirr_pct == null ? "—" : pct(t.xirr_pct)}</b></div>`,
      `<div><small>Stocks · buys</small><b>${t.n_stocks} · ${t.n_lots}</b></div>`] : [];
    $("#hSummary").innerHTML = cards.join("");
    const notes = [...(data.warns || []).map((w) => `⚠️ ${esc(w)}`)];
    if (data.fmv_pending) notes.push("⏳ Loading 31-Jan-2018 prices for your older buys (grandfathering)…");
    if ((data.dividend_refresh || []).length) notes.push("⏳ Fetching dividend history…");
    $("#hNotes").innerHTML = notes.map((n) => `<div>${n}</div>`).join("");
    $("#hNotes").hidden = !notes.length;

    const body = $("#hRows");
    body.innerHTML = "";
    const miss = data.missing || [];
    if (miss.length) {
      const head = document.createElement("tr");
      head.className = "miss-head";
      head.innerHTML = `<td colspan="7">📝 <b>${miss.length} in your watchlist without numbers yet</b> — with a date, type the
        quantity and price <b>as you bought them</b>; without one, what your broker shows today (see “How to enter” above).</td>`;
      body.append(head);
      for (const m of miss) body.append(missRow(m));
    }
    if (!data.stocks.length && !miss.length) {
      body.innerHTML = `<tr><td colspan="7" class="muted blank">No holdings yet — add a buy above, or put a
        <code>holdings.csv</code> in the project folder.</td></tr>`;
    }
    for (const s of data.stocks) {
      stockRow(s, body);
      for (const l of s.lots) if (!l.sold_out) body.append(lotRow(l));
    }
    renderRealised(data);
    const c = data.csv || {};
    $("#hCsv").innerHTML = c.status === "none"
      ? `Have many stocks? Put them in <code>${esc(c.path)}</code> — columns <code>symbol, qty, price, date</code> (date optional;
        add <code>side</code> = sell for sells). Your broker's holdings export works too. It's read whenever it changes.`
      : `📄 <code>${esc(c.path)}</code> — ${c.status === "imported" ? `just imported ${c.imported} row(s)` : "in sync"}.`
        + (c.errors && c.errors.length ? `<ul class="csv-err">${c.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "");
  }

  let retries = 0;
  async function load() {
    try {
      const data = await call("GET", "/api/holdings");
      render(data);
      clearTimeout(load.t);
      const pending = ["lookups", "merger_lookups", "dividend_refresh", "bse_action_refresh"]
        .some((k) => (data[k] || []).length) || data.fmv_pending;
      if (pending && retries < 6) { retries += 1; load.t = setTimeout(load, 20000); }   // background reads running
      else retries = 0;
    } catch (err) { flash(`Couldn't load holdings: ${err.message}`); }
  }

  // ── the add form: company-name search, "＋ Buy" / rights prefill ─────────────
  const form = $("#hForm"), box = $("#hSuggest");
  let picked = null, hits = [], active = -1, seq = 0;
  const hideBox = () => { box.hidden = true; active = -1; };

  function anotherBuy(s, prefill = {}) {
    picked = { symbol: s.symbol, name: s.name };
    hits = [picked];
    form.stock.value = s.name;
    if (prefill.qty) form.qty.value = prefill.qty;
    if (prefill.date) form.date.value = prefill.date;
    form.scrollIntoView({ behavior: "smooth", block: "center" });
    (prefill.qty ? form.price : form.qty).focus();
  }
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
    box.innerHTML = hits.map((h, i) => `<li role="option" data-i="${i}"><b>${esc(h.name)}</b><span>${h.note
      ? `<em class="${h.former ? "former" : "kind"}">${esc(h.note)}</em>` : esc(h.symbol)}</span></li>`).join("");
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
    const btn = $("button[type=submit]", form);
    if (!picked && hits.length === 1) picked = hits[0];
    btn.disabled = true;
    try {
      const lot = await call("POST", "/api/holdings", { stock: picked ? picked.symbol : form.stock.value,
        qty: form.qty.value, price: form.price.value, date: form.date.value || null });
      flash(`Added ${lot.name} — ${num(lot.qty, 4)} @ ${inr(lot.price, 2)}`, true);
      picked = null; hits = [];
      form.reset();
      form.stock.focus();
      load();
    } catch (err) { flash(err.message); }
    btn.disabled = false;
  });
  form.date.max = TODAY;

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
