"""Idea engines and screens on request: fundamental / technical screens, sectors, Tailwind, Pickaxe, hotlist, concalls, results, policy, investors."""

from __future__ import annotations

import threading
from datetime import datetime

from equity_research import config, scan
from equity_research.analysis import (
    accumulation,
    booking_risk,
    fundamental_screens,
    holdco,
    hotlist,
    investors,
    leaders,
    momentum,
    policy,
    screener,
    sector_analysis,
    smallcap,
    supply_chain,
    technical_screen,
)
from equity_research.bot.core import (
    IST,
    PENDING_TTL_H,
    _crore,
    _md_table,
    _MenuItem,
    _needs_llm,
    _re_subject,
    _reply_text,
    _screen_run,
    _set_pending,
    _track_push,
    log,
)
from equity_research.bot.queries import _wants_latest
from equity_research.common.db import connect
from equity_research.reports import (
    call_radar_brief,
    pickaxe_brief,
    results_brief,
    sector_brief,
    tailwind_brief,
)
from equity_research.reports import email as emailer
from equity_research.reports.inbox import EmailRequest
from equity_research.reports.pdf import report_to_pdf
from equity_research.reports.resolve import resolve


def _send_fundamental_screen(req: EmailRequest) -> None:
    """Ranked value+quality+forensic screen → a numbered list; reply a number → deep report."""
    log.info("running fundamental screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — running the quality + forensic + cheapness screen across "
                     "the Nifty-500 (~1–2 min). The ranked list will land in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: screener.fundamental_screen(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The screen timed out this time — please resend `screen: value` in a moment.")
        return
    if not rows:
        _reply_text(req, "No names scored — the universe's financials may not be ingested yet. "
                         "Run the one-time `backfill_universe.py` to seed the Nifty-500.")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Score", "Why"],
        [[i, r["symbol"], r["name"][:34], f"{r['composite']:.1f}", r["why"]]
         for i, r in enumerate(rows, 1)],
        align="rllrl")
    md = ("**🔎 Value + quality + forensic screen — Nifty-500**\n\n"
          "Composite 0-100: 40% quality · 35% forensic · 25% cheap-vs-own-history. "
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n"
          f"_The screen finds; your reply diligences. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]      # plain SYM → numbered reply → deep report
    _set_pending(req, "screen:value", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — value"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent fundamental screen (%d names) to %s", len(rows), req.sender)


def _send_sector_list(req: EmailRequest) -> None:
    """List the sectors the `sector:` command understands."""
    cat = sector_analysis.catalog()
    lines = [f"- {m['emoji']} **{k}** — {m['index']}" for k, m in cat.items()]
    md = ("**🧭 Sector analysis — available sectors**\n\n"
          "Send `sector: <name>` (e.g. `sector: defence`, `sector: pharma`, `sector: realty`) for a "
          "top-down read: index trend + valuation vs its own history, who's accumulating it, and the "
          "best / cheapest names inside it (reply a number for a stock's deep report).\n\n"
          + "\n".join(lines))
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Sectors"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)


def _send_sector_rotation(req: EmailRequest) -> None:
    """On-demand sector-rotation digest (leaders / laggards / value-turning across all sectors)."""
    log.info("running sector rotation (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — ranking all sectors by relative strength vs Nifty + valuation "
                     "vs their own history. Lands in this thread shortly.")
    con = connect()
    try:
        body = _screen_run(lambda: sector_brief.build_sector_rotation(con))
    finally:
        con.close()
    if not body:
        _reply_text(req, "Couldn't rank the sectors this time — please resend `sector: rotation`.")
        return
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Sector rotation"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sector rotation to %s", req.sender)


def _send_sector_analysis(req: EmailRequest, canonical: str) -> None:
    """Top-down sector report → numbered top/undervalued list; reply a number → deep report."""
    meta = sector_analysis.catalog()[canonical]
    log.info("running sector analysis '%s' (req from %s)", canonical, req.sender)
    _reply_text(req, f"📩 Got it — pulling the top-down read on {meta['emoji']} "
                     f"{meta['index'].replace('Nifty ', '')}: index trend + valuation vs its own "
                     f"history, who's accumulating it, and the best / cheapest names inside it "
                     f"(~1–2 min). The report will land in this thread.")
    con = connect()
    try:
        report = _screen_run(lambda: sector_brief.build_sector_report(con, canonical))
    finally:
        con.close()
    if not report:
        _reply_text(req, f"Couldn't build that sector this time (timed out or no index data) — "
                         f"please resend `sector: {canonical}` in a moment.")
        return
    body = report["markdown"]
    cands = [_MenuItem(p["symbol"], p["name"]) for p in report["picks"]]  # plain SYM → reply → deep report
    _set_pending(req, f"sector:{canonical}", cands)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, f"Sector — {report['sector_name']}"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sector analysis '%s' (%d picks) to %s", canonical, len(cands), req.sender)


def _send_tailwind(req: EmailRequest) -> None:
    """💨 Tailwind — global supply/policy shocks → verified Indian beneficiaries; reply → deep report.
    Serves the 24h cache by default; `tailwind --latest` (or `fresh`) forces a live re-scan."""
    if _needs_llm(req, "Tailwind"):
        return
    latest = _wants_latest(req.subject)
    log.info("running Tailwind (req from %s, latest=%s)", req.sender, latest)
    _reply_text(req, "📩 Got it — pulling the latest global supply-shock scan and the Indian names "
                     "that benefit. Forcing a fresh live scan (~2–4 min); it lands in this thread."
                     if latest else
                     "📩 Got it — fetching the global supply-shock scan (cached for 24h, so this is "
                     "quick unless it's stale; add `--latest` to force a fresh live scan). Lands in "
                     "this thread shortly.")
    con = connect()
    try:
        rep = _screen_run(lambda: tailwind_brief.build_tailwind_report(con, use_cache=not latest) or {},
                          timeout=config.TAILWIND_TIMEOUT_S)
    finally:
        con.close()
    if rep is None:                                        # timed out / failed — not "nothing found"
        _reply_text(req, "The Tailwind scan timed out this time — please resend `tailwind` shortly.")
        return
    if not rep:
        _reply_text(req, "No clean global supply-shock → Indian-beneficiary setup surfaced right now "
                         "(nothing fresh crossed the bar, or no verifiable listed name). That's a "
                         "valid answer — I don't force one. Try again in a day or two.")
        return
    body = rep["markdown"]
    if rep.get("from_cache"):                              # note that this is a reused (not live) scan
        stamp = ""
        try:
            ca = datetime.fromisoformat(rep["cached_at"]).astimezone(IST)
            stamp = f" from {ca:%d-%b %H:%M}"
        except Exception:  # noqa: BLE001
            pass
        body = (f"> ♻️ _Cached scan{stamp} (within 24h — same data as the last run). Reply "
                f"`tailwind --latest` for a fresh live scan._\n\n" + body)
    _set_pending(req, "tailwind", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Tailwind"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent Tailwind (%d catalysts, %d picks, cache=%s) to %s",
             rep["n_catalysts"], len(rep["picks"]), rep.get("from_cache", False), req.sender)


# Pickaxe is deep (on-demand financials ingest + a filing-grounded read per name + Trends charts),
# so a full run is ~10-15 min — well past the IMAP loop's tolerance. It therefore runs in a
# BACKGROUND daemon thread: the command is acked instantly and the report+PDF lands when ready. The
# lock ensures only ONE build runs at a time (the on-demand command and the weekly push can't overlap
# and hammer the DB together).
_pickaxe_lock = threading.Lock()


def _pickaxe_pdf(body: str, images: list) -> bytes | None:
    if not images:
        return None
    try:
        return report_to_pdf(body, "Pickaxe", images)
    except Exception:  # noqa: BLE001 — a PDF failure must not lose the report; send body-only
        log.exception("pickaxe PDF render failed — sending body-only")
        return None


def _pickaxe_worker(*, req: EmailRequest | None, to: str, subject: str,
                    use_cache: bool, weekly: bool) -> None:
    """Build the deep Pickaxe report in the background and deliver it (report body + charted PDF).
    ``req`` present → on-demand (threads the reply + arms the numbered reply→deep-report menu);
    ``weekly`` → the Saturday push (advances the week-marker only after a successful send)."""
    if not _pickaxe_lock.acquire(blocking=False):
        log.info("pickaxe: a build is already running — skipping this trigger")
        if req is not None:
            _reply_text(req, "⏳ A Pickaxe scan is already running — your result will land shortly.")
        return
    try:
        con = connect()
        try:
            rep = pickaxe_brief.build_pickaxe_report(con, use_cache=use_cache)
        finally:
            con.close()
        if not rep:
            if weekly:
                scan.mark_pickaxe()
                log.info("pickaxe: nothing surfaced this week — no email")
            elif req is not None:
                _reply_text(req, "No durable surging-demand → Indian-beneficiary setup surfaced right "
                                 "now. That's a valid answer — I don't force one. Try again in a day.")
            return
        body = rep["markdown"]
        if rep.get("from_cache"):
            stamp = ""
            try:
                ca = datetime.fromisoformat(rep["cached_at"]).astimezone(IST)
                stamp = f" from {ca:%d-%b %H:%M}"
            except Exception:  # noqa: BLE001
                pass
            body = (f"> ♻️ _Cached scan{stamp} (within 24h — same data as the last run). Reply "
                    f"`pickaxe --latest` for a fresh live scan._\n\n" + body)
        pdf = _pickaxe_pdf(body, rep.get("images") or [])
        today = datetime.now(IST).date().isoformat()
        attachments = [(f"Pickaxe_{today}.pdf", pdf)] if pdf else []
        if req is not None:
            _set_pending(req, "pickaxe", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
            emailer.send_report(subject, body, to=to, html=emailer.body_html(body, "Pickaxe"),
                                attachments=attachments, images=rep.get("images") or [],
                                in_reply_to=req.message_id,
                                references=req.references or req.message_id)
        else:
            emailer.send_report(subject, body, to=to, html=emailer.body_html(body, "Pickaxe"),
                                attachments=attachments, images=rep.get("images") or [])
            _track_push("pickaxe", rep, subject)
        if weekly:
            scan.mark_pickaxe()
        log.info("sent Pickaxe (%d themes, %d picks, pdf=%s, cache=%s) to %s",
                 rep["n_themes"], len(rep["picks"]), bool(pdf), rep.get("from_cache", False), to)
    except Exception:  # noqa: BLE001 — the worker owns its errors; never crash the loop
        log.exception("pickaxe worker failed")
        if req is not None:
            try:
                _reply_text(req, "⚠️ The Pickaxe scan hit an error mid-build — please try again.")
            except Exception:  # noqa: BLE001
                pass
    finally:
        _pickaxe_lock.release()


def _send_pickaxe(req: EmailRequest) -> None:
    """⛏️ Pickaxe — surging Indian demand → the indirect 'sell the pickaxes' beneficiary. Deep run
    (~10-15 min): ack now, deliver the full report + charted PDF from a background thread when ready.
    Serves the 24h cache by default; `pickaxe --latest` forces a fresh live scan."""
    if _needs_llm(req, "Pickaxe"):
        return
    latest = _wants_latest(req.subject)
    log.info("queuing Pickaxe build (req from %s, latest=%s)", req.sender, latest)
    _reply_text(req, "📩 Got it — running the deep demand scan: rising 'buy' searches → durable "
                     "themes → the indirect names that ride them, each with exact price/P/E/levels and "
                     "a filing-grounded revenue-share & forward projection, plus Google-Trends charts. "
                     "This is a heavy build (~10-15 min); the full report + PDF lands in this thread "
                     "when ready." + ("" if latest else " (Cached within 24h; add `--latest` to force fresh.)"))
    # Name on-demand delivery threads '*-ondemand': the CLI / web UI wait for exactly those
    # (bot.local.LocalSession) so a background-delivered report isn't lost when the command ends.
    threading.Thread(
        target=_pickaxe_worker,
        kwargs={"req": req, "to": req.sender, "subject": _re_subject(req.subject),
                "use_cache": not latest, "weekly": False},
        name="pickaxe-ondemand", daemon=True).start()


def _send_suppliers(req: EmailRequest, query: str) -> None:
    """Map ONE company's smaller listed suppliers/ancillaries → numbered list; reply → deep report."""
    if _needs_llm(req, "The supplier map"):
        return
    cand = resolve(query)
    if not cand:
        _reply_text(req, f"Couldn't resolve '{query}' to an NSE company — try the exact name or symbol.")
        return
    target = cand[0]
    log.info("running supplier map for %s (req from %s)", target.symbol, req.sender)
    _reply_text(req, f"📩 Got it — mapping the smaller **listed** suppliers / ancillaries feeding "
                     f"{target.name} ({target.symbol}). ~1 min; the list lands in this thread.")
    con = connect()
    try:
        sup = _screen_run(lambda: supply_chain.suppliers_for_company(con, target.symbol, target.name))
    finally:
        con.close()
    if not sup:
        _reply_text(req, f"No **listed** suppliers/ancillaries surfaced for {target.name} that I could "
                         f"verify on NSE. (This maps listed vendors only — many suppliers are private.)")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Supplies", "Source"],
        [[i, r["symbol"], r["name"][:28], (r.get("role") or "")[:40],
          "🖐️ curated" if r["source"] == "curated" else "🤖 AI"] for i, r in enumerate(sup, 1)],
        align="rllll")
    body = (f"**🔗 Supply chain — {target.name} ({target.symbol})**\n\n"
            "Smaller **listed** companies that supply / make components for it — the *indirect* "
            "beneficiaries beyond the marquee name. 🖐️ hand-curated · 🤖 **AI-suggested, verify** the "
            "link before acting. **Reply a number for that stock's full deep report.**\n\n" + table +
            f"\n\n_Listed vendors only — a discovery aid, not a confirmed supplier ledger. "
            f"(Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, f"suppliers:{target.symbol}", [_MenuItem(r["symbol"], r["name"]) for r in sup])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, f"Suppliers — {target.symbol}"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent supplier map for %s (%d names) to %s", target.symbol, len(sup), req.sender)


def _send_booking_risk(req: EmailRequest) -> None:
    """Heads-up on YOUR holdings where institutions sit on big gains (elevated selling risk)."""
    log.info("running portfolio booking-risk (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — checking your holdings for where the tracked institutions are "
                     "sitting on big gains (profit-booking risk). Lands in this thread shortly.")
    con = connect()
    try:
        res = _screen_run(lambda: booking_risk.portfolio_booking_risk(con, "holding"))
    finally:
        con.close()
    if res is None:
        _reply_text(req, "That scan timed out this time — please resend `booking` shortly.")
        return
    scored, nodata = res["scored"], res["nodata"]
    if not scored:
        _reply_text(req, "None of your holdings have enough shareholding history yet to estimate "
                         "an institutional cost. (Deepens as SHP history is ingested.)")
        return

    def _risk(r):
        return f"{r['emoji']} {r['avg_gain']:+.0f}%"

    def _who(r):
        t = r.get("top_holder")
        return (f"{t['name']} {t['gain_pct']:+.0f}%" if t else "—")
    rows = [[i, r["symbol"], r["name"], _risk(r), str(r["n_high"]), _who(r)]
            for i, r in enumerate(scored, 1)]
    table = _md_table(["#", "Symbol", "Company", "Instns' avg gain", "≥50% holders", "Deepest holder"],
                      rows, align="rlllrl")
    high = [r for r in scored if r["avg_gain"] >= 50]
    lead = (f"**{len(high)} of your {len(scored)} scored holdings** have institutions sitting on "
            f"**≥50% gains** — watch those for profit-booking pressure." if high
            else "None of your holdings show institutions on big (≥50%) gains right now.")
    body = ("**⚠️ Profit-booking risk — your holdings**\n\n" + lead + " Ranked by the stake-weighted "
            "gain the **tracked institutions** are sitting on (inferred from the price zones of the "
            "quarters they added in — exact prices aren't disclosed). Highest = deepest in profit = "
            "most likely to book. **Reply a number for that stock's deep report.**\n\n" + table)
    if nodata:
        body += ("\n\n_No institutional cost estimate yet for: "
                 + ", ".join(r["symbol"] for r in nodata[:20])
                 + " (held before our data / thin shareholding history — deepens as more is ingested)._")
    body += ("\n\n_A heads-up, not a sell signal — a strong stock can keep running past institutional "
             f"cost. Decision support; the call is yours. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in scored] + \
            [_MenuItem(r["symbol"], r["name"]) for r in nodata]
    _set_pending(req, "booking", cands)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Booking risk — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent booking-risk (%d scored, %d nodata) to %s", len(scored), len(nodata), req.sender)


def _send_smallcap_screen(req: EmailRequest) -> None:
    """Capex-led small-cap discovery screen → a numbered list; reply a number → deep report."""
    log.info("running small-cap screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — hunting strong small-caps (₹1,000–10,000 cr) led by the "
                     "**capex cycle**, with traps gated out. ~1–2 min; the ranked list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: smallcap.smallcap_screen(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The small-cap screen timed out this time — please resend `screen: smallcap`.")
        return
    if not rows:
        _reply_text(req, "No small-caps scored yet — the small-cap universe's financials may not be "
                         "ingested. Run `backfill_universe.py --seed-smallcaps --only-missing` first.")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Score", "M-cap", "Capex", "Why"],
        [[i, r["symbol"], r["name"][:26], f"{r['composite']:.1f}", f"{r['mcap']:,.0f}",
          (f"{r['capex_growth']:.1f}×" if r.get("capex_growth") else "—"), r["why"]]
         for i, r in enumerate(rows, 1)],
        align="rllrrll")
    md = ("**🚀 Small-cap capex-cycle screen — ₹1,000–10,000 cr**\n\n"
          "Composite 0-100: **30% capex cycle** (capex vs its 3y base · capex÷depr · self-funded) · "
          "25% capital efficiency (ROCE & trend) · 20% cash/balance-sheet · 15% forensic · "
          "10% smart-money — with near-distress / manipulation / heavy-pledge / shrinking-revenue "
          "names **gated out**. Valuation shown for context, not scored. "
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n"
          f"_The screen finds; your reply diligences. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "screen:smallcap", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — small-cap capex"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent small-cap screen (%d names) to %s", len(rows), req.sender)


_TA_LEGEND = (
    "\n\n---\n\n**📖 What the terms mean** (plain English — this screen is chart-based, so here's the jargon)\n\n"
    "- **Uptrend (>200-DMA):** price is above its average of the last 200 days — the long-term trend is up. "
    "**50>200:** the 50-day average sits above the 200-day one (a 'golden cross') — momentum backs the trend.\n"
    "- **RS vs Nifty (relative strength):** how much the stock beat or lagged the Nifty index over ~3 months. "
    "'+40%' = it outran the market by 40%.\n"
    "- **MACD+:** a popular momentum indicator that's turned positive — recent momentum is bullish.\n"
    "- **RSI:** a 0–100 'how stretched' gauge. ~40–60 is healthy; **>70 = overbought** (stretched — risky to chase).\n"
    "- **% below 52w high:** how far under its 1-year peak it is. Near 0% = knocking on a breakout.\n"
    "- **Delivery spike:** an unusually high share of volume was actually *delivered* (bought to hold, not day-traded) "
    "— a sign of real conviction.\n"
    "- **Buy zone (support):** the price range to buy **on a small dip** — the nearest *support* (a level buyers have "
    "defended before). Don't chase above it; wait for the pullback.\n"
    "- **Stop:** where you sell if you're wrong (just below support) — it caps the loss. "
    "**Target (resistance):** the next *resistance* (a level sellers have defended) — where to consider booking profit.\n"
    "- **Reward:risk (R:R):** potential profit ÷ potential loss. '2.4:1' = aiming to make ₹2.40 for every ₹1 at risk. "
    "Higher is better — we only label it **accumulate** at ≥1.5:1.\n"
    "- **Setup type:** *accumulate* = clean buy-the-dip (R:R ≥ 1.5) · *breakout* = at new highs with no ceiling above, "
    "so trail a stop instead of a fixed target · *watch* = the shape is there but reward:risk is thin, so wait for a "
    "better entry.\n\n"
    "_A **candidate finder with defined risk**, not a promise — it hands you an entry, a stop and a target so your "
    "downside is always capped. Reply a number to get the full company report before you buy._"
)


def _send_technical_screen(req: EmailRequest) -> None:
    """Technical-setup discovery screen → the strongest chart setups to buy, market-wide, each
    with an entry zone / stop / target / reward:risk. Reply a number → that name's deep report."""
    log.info("running technical screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning the liquid universe for the strongest **technical setups** "
                     "(trend · relative strength · momentum), trap-gated, with entry/stop/target. "
                     "~1 min; the ranked list lands in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: technical_screen.technical_screen(con, limit=config.TECHNICAL_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The technical screen timed out this time — please resend `screen: technical`.")
        return
    if not rows:
        _reply_text(req, "No clean setups cleared the liquidity + safety gate today — the universe's "
                         "financials may not be ingested yet (run `backfill_universe.py`), or the "
                         "tape simply has no strong, un-extended setups right now.")
        return

    def _rng(lo, hi):
        return f"₹{lo:,.0f}–₹{hi:,.0f}" if lo is not None else "—"
    tbl = []
    for i, r in enumerate(rows, 1):
        tgt = f"₹{r['target']:,.0f}" if r["target"] else ("trail" if r["kind"] == "breakout" else "—")
        rr = f"{r['rr']:.1f}:1" if r["rr"] else "—"
        tbl.append([i, r["symbol"], r["name"][:18], r["kind"], f"₹{r['price']:,.0f}",
                    _rng(r["entry_lo"], r["entry_hi"]), f"₹{r['stop']:,.0f}", tgt, rr, r["why"]])
    table = _md_table(
        ["#", "Symbol", "Company", "Setup", "Price", "Buy zone", "Stop", "Target", "R:R", "Why"],
        tbl, align="rlllrrrrll")
    md = ("**📈 Technical setups — strongest charts to buy (market-wide)**\n\n"
          "Ranked on **price action**: 30% trend (>200-DMA · 50>200) · 25% relative strength vs "
          "Nifty · 15% MACD · 10% RSI-health · 10% breakout proximity · 10% delivery. Only liquid "
          "names (≥₹2 cr/day) that **clear a trap gate** (no Altman-distress / Beneish-manipulator / "
          "heavy pledge) and sit near a **buyable** support surface. **Buy zone** = a pullback to the "
          "nearest support; **stop** below it; **target** = next resistance (`trail` = breakout, no "
          "overhead). `accumulate` R:R≥1.5 · `breakout` blue-sky · `watch` = thin R:R. "
          "**Reply a number for that name's full deep report before you act.**\n\n"
          + table + "\n\n"
          "_Candidate finder with **defined risk**, not a back-tested edge — short-term timing is the "
          "least-proven part of the tool. Bounded to symbols with financials ingested (so the safety "
          f"gate is real); coverage grows with the backfill. (Reply within {PENDING_TTL_H}h.)_"
          + _TA_LEGEND)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "screen:technical", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — technical setups"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent technical screen (%d names) to %s", len(rows), req.sender)


def _send_volume_screen(req: EmailRequest) -> None:
    """Volume Breakouts — stocks near a 52-week high, in an uptrend, with a volume surge, market-wide.
    Reply a number → that name's deep report."""
    log.info("running volume-breakouts screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning the market for **Volume Breakouts** (near 52-week highs, "
                     "uptrend intact, with a volume surge). ~1 min; the ranked list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: momentum.scan(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The Volume Breakouts screen timed out this time — please resend `screen: volume`.")
        return
    if not rows:
        _reply_text(req, "No clean breakouts cleared the filters today — the tape may simply have no "
                         "strong, volume-backed names near their highs right now.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"₹{r['price']:,.0f}", r["breakout"],
            f"{r['vol_surge']:.1f}×", f"{r['deliv_ratio']:.1f}×", (r["sector"] or "—")[:16]]
           for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "Price", "Breakout", "Vol", "Deliv", "Sector"],
                      tbl, align="rllrllll")
    body = ("**📊 Volume Breakouts — new highs backed by a volume surge (market-wide)**\n\n"
            "Names within ~4% of their **52-week high**, in an uptrend (>200-DMA · 50>200), with a "
            "**volume surge** confirming the move. **Vol** = latest volume ÷ its 20-day average; "
            "**Deliv** = delivery% ÷ its 20-day average (>1 = stronger conviction). Ranked across the "
            "full liquid equity universe — a name without ingested financials is still shown (its "
            "report ingests on demand). **Reply a number for that name's full deep report.**\n\n"
            + table + f"\n\n_A discovery screen — a candidate finder, not a call. Breakouts fail; "
            f"diligence each name before acting. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:volume", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — volume breakouts"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent volume-breakouts screen (%d names) to %s", len(rows), req.sender)


def _send_beaters_screen(req: EmailRequest) -> None:
    """Market Beaters — names beating the Nifty 500 over 3/6/12m, still trending up, market-wide.
    Reply a number → that name's deep report."""
    log.info("running market-beaters screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — finding the **Market Beaters** (beating the Nifty 500 over "
                     "3/6/12 months, still trending up). ~1 min; the list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: leaders.scan(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The Market Beaters screen timed out this time — please resend `screen: beaters`.")
        return
    if not rows:
        _reply_text(req, "No names are meaningfully beating the market on the filters right now.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"₹{r['price']:,.0f}", f"{r['ret_3m']:+.0f}%",
            f"{r['ret_6m']:+.0f}%", f"{r['ret_1y']:+.0f}%", f"{r['out_3m']:+.0f}pp",
            (r["sector"] or "—")[:14]] for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "Price", "3m", "6m", "1y", "vs Nifty500 (3m)", "Sector"],
                      tbl, align="rllrrrrrl")
    body = ("**🏆 Market Beaters — quietly outrunning the market (market-wide)**\n\n"
            "Names **beating the Nifty 500** over 3, 6 and 12 months and still above their 200-DMA — "
            "relative-strength leadership tends to persist. **vs Nifty500 (3m)** = the name's 3-month "
            "return minus the index's, in percentage points. Only liquid, tradeable names. **Reply a "
            "number for that name's full deep report.**\n\n"
            + table + f"\n\n_A discovery screen — a candidate finder, not a call. Strong runs can be "
            f"extended; diligence each name before acting. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:beaters", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — market beaters"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent market-beaters screen (%d names) to %s", len(rows), req.sender)


def _send_institutions_screen(req: EmailRequest) -> None:
    """Institutional Buying — where promoters raised their stake QoQ (with the institutions adding
    alongside). Reply a number → that name's deep report."""
    log.info("running institutional-buying screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning shareholding filings for **Institutional Buying**: where "
                     "promoters raised their own stake last quarter, and who's adding alongside. ~1 min.")
    con = connect()
    try:
        rows = _screen_run(lambda: accumulation.scan(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The Institutional Buying screen timed out this time — please resend "
                         "`screen: institutions`.")
        return
    if not rows:
        _reply_text(req, "No clear promoter/institutional buying surfaced in the latest shareholding "
                         "filings.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"₹{r['price']:,.0f}" if r["price"] else "—",
            f"+{r['promoter_delta']:.2f}pp", f"{r['promoter_now']:.1f}%", r["added"], r["as_of"]]
           for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "Price", "Promoter Δ", "Now", "Top adder", "As of"],
                      tbl, align="rllrrrll")
    body = ("**🏛️ Institutional Buying — where insiders & institutions are adding**\n\n"
            "Names where the **promoter raised their own stake** quarter-on-quarter — buying with their "
            "own money is one of the more reliable signals there is — with the largest **institution "
            "adding alongside** them. **Promoter Δ** = change in promoter holding vs the prior quarter "
            "(percentage points). **Reply a number for that name's full deep report.**\n\n"
            + table + "\n\n_Bounded to names with holder-level shareholding ingested (coverage grows "
            f"over time). A discovery screen, not a call. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:institutions", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — institutional buying"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent institutional-buying screen (%d names) to %s", len(rows), req.sender)


def _send_margins_screen(req: EmailRequest) -> None:
    """Margin Momentum — net margin expanding (vs the prior ~4 quarters) on growing revenue."""
    log.info("running margin-momentum screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning for **Margin Momentum** (net margin expanding on growing "
                     "revenue). ~1 min; the ranked list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: fundamental_screens.margin_momentum(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The margins screen timed out this time — please resend `screen: margins`.")
        return
    if not rows:
        _reply_text(req, "No names with a clean, growth-backed margin expansion cleared the filters today.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"{r['net_margin']:.1f}%", f"+{r['margin_bps']} bps",
            f"{r['rev_yoy']:+.0f}%", (r["sector"] or "—")[:16]] for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "Net margin", "Δ margin", "Rev YoY", "Sector"],
                      tbl, align="rllrrrl")
    body = ("**📈 Margin Momentum — expanding margins on growing revenue**\n\n"
            "Names whose **net margin** is widening vs the prior four quarters **while revenue grows** "
            "(margin gains on a growing base, not a shrinking one). **Δ margin** is the expansion in "
            "basis points (100 bps = 1 percentage point). Forensic-trap-gated. **Reply a number for that "
            "name's full deep report.**\n\n" + table +
            f"\n\n_A discovery screen, not a call. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:margins", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — margin momentum"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent margin-momentum screen (%d names) to %s", len(rows), req.sender)


def _send_deleverage_screen(req: EmailRequest) -> None:
    """Debt Payers — cut debt over ~3-4 years while staying profitable (healthy deleveraging)."""
    log.info("running debt-payers screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning for **Debt Payers** (companies cutting debt over the years "
                     "while staying profitable). ~1 min; the list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: fundamental_screens.debt_payers(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The deleverage screen timed out this time — please resend `screen: deleverage`.")
        return
    if not rows:
        _reply_text(req, "No clean healthy-deleveraging names cleared the filters today.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"{r['de']:.2f}" if r["de"] is not None else "—",
            f"−{r['debt_cut_pct']}%", f"{r['roce']:.0f}%", (r["sector"] or "—")[:16]]
           for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "D/E now", "Debt cut (3-4y)", "ROCE", "Sector"],
                      tbl, align="rllrrrl")
    body = ("**💸 Debt Payers — cutting debt while staying profitable**\n\n"
            "Names that have **reduced total borrowings** over the last 3-4 years **and** still earn a "
            "healthy return (ROCE > 0) — deleveraging from strength, not distress. **D/E now** = "
            "debt ÷ equity today; **Debt cut** = the fall in total borrowings. **Reply a number for that "
            "name's full deep report.**\n\n" + table +
            f"\n\n_A discovery screen, not a call. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:deleverage", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — debt payers"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent debt-payers screen (%d names) to %s", len(rows), req.sender)


def _send_quality_screen(req: EmailRequest) -> None:
    """Compounders — high ROCE, low debt, steady multi-year growth, clean books."""
    log.info("running compounders screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning for **Compounders** (high ROCE, low debt, steadily growing, "
                     "clean books). ~1-2 min; the list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: fundamental_screens.compounders(con, limit=config.SCREEN_RESULT_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The quality screen timed out this time — please resend `screen: quality`.")
        return
    if not rows:
        _reply_text(req, "No names cleared the high-ROCE / low-debt / steady-growth bar today.")
        return
    tbl = [[i, r["symbol"], r["name"][:20], f"{r['roce']:.0f}%", f"{r['de']:.2f}" if r["de"] is not None else "—",
            f"{r['cagr']:.0f}%", f"{r['forensic']:.1f}/4" if r["forensic"] is not None else "—",
            (r["sector"] or "—")[:14]] for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "ROCE", "D/E", "3y rev CAGR", "Forensic", "Sector"],
                      tbl, align="rllrrrll")
    body = ("**🏆 Compounders — high-return, low-debt, steady growers**\n\n"
            "The **quality** screen: names with a **high ROCE** (≥15%), **low debt** (D/E ≤ 0.75) and "
            "**steady multi-year revenue growth**, with clean forensics. Leads with *business quality* "
            "(unlike `screen: value`, which leads with cheapness). **Forensic** = a 0-4 health score "
            "(Altman · Beneish · accruals · no-pledge). **Reply a number for that name's full deep "
            "report.**\n\n" + table +
            f"\n\n_A discovery screen, not a call — quality says nothing about price. (Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:quality", [_MenuItem(r["symbol"], r["name"]) for r in rows])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Screen — compounders"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent compounders screen (%d names) to %s", len(rows), req.sender)


def _hotlist_md(rows: list[dict]) -> str:
    """Render the Hotlist rows to the emailed markdown (table + explainer)."""
    tbl = [[i, r["symbol"], r["name"][:20], "●" * r["n_signals"] + f" {r['n_signals']}",
            ", ".join(r["engines"]), f"₹{r['price']:,.0f}" if r.get("price") else "—",
            (r["sector"] or "—")[:14]] for i, r in enumerate(rows, 1)]
    table = _md_table(["#", "Symbol", "Company", "Signals", "Flagged by", "Price", "Sector"],
                      tbl, align="rllllrl")
    return ("**🔥 Hotlist — names lighting up across multiple discovery engines**\n\n"
            "Every screen surfaces a list; what matters is **confluence**. This ranks names by **how "
            "many engines flag the same stock** — Volume Breakouts, Market Beaters, Institutional "
            "Buying, value+forensic, and small-cap capex. A name flagged by several at once is a higher-"
            "conviction lead than one flagged by a single screen. **Reply a number for that name's "
            "full deep report.**\n\n" + table + f"\n\n_A discovery screen — a candidate finder, not a "
            f"call. (Reply within {PENDING_TTL_H}h.)_")


def _send_hotlist(req: EmailRequest) -> None:
    """🔥 Hotlist — names surfaced by the most discovery engines at once. Serves the 24h cache by
    default; `hotlist --latest` forces a fresh multi-engine build. Reply a number → deep report."""
    latest = _wants_latest(req.subject)
    log.info("running Hotlist (req from %s, latest=%s)", req.sender, latest)
    _reply_text(req, "📩 Got it — building the **Hotlist** (a fresh run across every discovery engine, "
                     "~2–3 min); it lands in this thread." if latest else
                     "📩 Got it — fetching the **Hotlist** (cached for 24h, so this is quick unless "
                     "it's stale; add `--latest` to force a fresh run). Lands here shortly.")
    con = connect()
    from_cache, cached_at = False, None
    try:
        cached = None if latest else scan.hotlist_cache_get(con)
        if cached and cached.get("report", {}).get("picks"):
            rep, from_cache, cached_at = cached["report"], True, cached.get("cached_at")
        else:
            rows = _screen_run(lambda: hotlist.build(con, limit=config.HOTLIST_LIMIT),
                               timeout=config.HOTLIST_TIMEOUT_S)
            if rows is None:
                _reply_text(req, "The Hotlist timed out this time — please resend `hotlist` shortly.")
                return
            rep = {"markdown": _hotlist_md(rows),
                   "picks": [{"symbol": r["symbol"], "name": r["name"]} for r in rows]}
            if rows:
                scan.hotlist_cache_put(rep, con)
    finally:
        con.close()
    if not rep.get("picks"):
        _reply_text(req, "Nothing is lighting up across multiple engines right now — a valid answer. "
                         "Try again in a day or two.")
        return
    body = rep["markdown"]
    if from_cache:
        stamp = ""
        try:
            ca = datetime.fromisoformat(cached_at).astimezone(IST)
            stamp = f" from {ca:%d-%b %H:%M}"
        except Exception:  # noqa: BLE001
            pass
        body = (f"> ♻️ _Cached run{stamp} (within 24h — same names as the last build). Reply "
                f"`hotlist --latest` for a fresh run._\n\n" + body)
    _set_pending(req, "hotlist", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Hotlist"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent Hotlist (%d names, cache=%s) to %s", len(rep["picks"]), from_cache, req.sender)


def _send_call_radar(req: EmailRequest) -> None:
    """🎙️ Concalls — the most notable recent earnings calls (forward tone vs delivered numbers),
    read from the pre-scored `concall_signals` table (no LLM at request time). Reply a number →
    that name's deep report."""
    log.info("running Concalls (req from %s)", req.sender)
    con = connect()
    try:
        rep = call_radar_brief.build_call_radar(con)
    finally:
        con.close()
    if not rep or not rep.get("picks"):
        _reply_text(req, "🎙️ No earnings calls have been scored in the recent window yet — the radar "
                         "fills in as companies file transcripts (heaviest during results season). "
                         "Try again in a bit.")
        return
    _set_pending(req, "calls", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
    emailer.send_report(_re_subject(req.subject), rep["markdown"], to=req.sender,
                        html=emailer.body_html(rep["markdown"], "Concalls"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent Concalls (%d calls) to %s", len(rep["picks"]), req.sender)


def _send_results(req: EmailRequest) -> None:
    """📈 Results Radar — the strongest just-reported quarters (growth + acceleration), computed from
    the numbers. Reply a number → that name's deep report."""
    log.info("running Results Radar (req from %s)", req.sender)
    con = connect()
    try:
        # `or {}`: the builder returns None when nobody reported in the window — keep None for a
        # real timeout/error so the two get honest, different replies
        rep = _screen_run(lambda: results_brief.build_results(con) or {})
    finally:
        con.close()
    if rep is None:
        _reply_text(req, "The Results Radar timed out this time — please resend `results` shortly.")
        return
    if not rep.get("picks"):
        _reply_text(req, "📈 No companies have reported in the recent window yet — the radar fills in "
                         "as results are filed (heaviest during earnings season). Try again in a bit.")
        return
    _set_pending(req, "results", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
    emailer.send_report(_re_subject(req.subject), rep["markdown"], to=req.sender,
                        html=emailer.body_html(rep["markdown"], "Results Radar"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent Results Radar (%d names) to %s", len(rep["picks"]), req.sender)


def _send_policy_screen(req: EmailRequest) -> None:
    """Government policy / scheme radar — schemes in the latest PIB (primary) releases, with the
    sector(s) they hit and likely listed beneficiaries (watchlist names flagged). Standalone
    screen; no effect on reports/watchlist/digests."""
    if _needs_llm(req, "The policy radar"):
        return
    log.info("running policy radar (req from %s)", req.sender)
    _reply_text(req, "🏛️ Got it — scanning the latest **government press releases (PIB, primary "
                     "source)** for new schemes/policies and mapping each to the sectors and "
                     "listed companies it affects. ~1 min; the list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: policy.policy_scan(con, limit_releases=config.POLICY_LIMIT_RELEASES),
                           timeout=config.POLICY_TIMEOUT_S)
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The policy radar timed out this time — please resend `screen: policy`.")
        return
    if not rows:
        _reply_text(req, "No market-relevant government schemes in the latest PIB releases right "
                         "now — try again later (the feed refreshes through the day).")
        return
    parts = ["## 🏛️ Government policy radar",
             "_Schemes & policies from **primary government press releases (PIB)** that move a "
             "**listed sector** — often at the **announced / cabinet-approved / draft / "
             "consultation** stage, before formal launch. Most **watchlist-relevant first**; "
             "⭐ = a name you hold/track. A discovery screen — it defers to each stock's own "
             "fundamentals, so reply with any symbol for its full deep report._"]
    for i, s in enumerate(rows, 1):
        parts.append("---")
        tag_bits = []
        if s.get("ministry"):
            tag_bits.append(f"🏛️ **{s['ministry']}**")
        if s.get("stage"):
            tag_bits.append(f"📅 _{s['stage']}_")
        if s.get("confidence"):
            tag_bits.append(f"🎯 _{s['confidence']} confidence_")
        parts.append(f"### {i}. {s['scheme']}")
        if tag_bits:
            parts.append(" · ".join(tag_bits))
        if s.get("sectors"):
            parts.append("🧭 **Sectors:** " + ", ".join(f"**{x}**" for x in s["sectors"])
                         + (f"  ·  ⚙️ **Mechanism:** {s['mechanism']}" if s.get("mechanism") else ""))
        if s.get("what_it_is"):
            parts.append(f"📄 **What it is:** {s['what_it_is']}")
        if s.get("benefit"):
            parts.append(f"💡 **Why it matters:** {s['benefit']}")
        bens = s.get("beneficiaries") or []
        listed = [b for b in bens if b["symbol"]]
        others = [b for b in bens if not b["symbol"]]
        if listed:
            parts.append("🎯 **Likely beneficiaries (NSE-listed):**")
            lines = []
            for b in listed[:12]:
                star = " ⭐" if b["on_watchlist"] else ""
                why = f" — {b['why']}" if b.get("why") else ""
                lines.append(f"- **{b['name']}** ({b['symbol']}){star}{why}")
            parts.append("\n".join(lines))
        if others:
            parts.append("_Also flagged (not matched to an NSE symbol): _"
                         + ", ".join(b["name"] for b in others[:8]))
        parts.append(f"🔗 _Source: PIB — {s['link']}_")
    md = "\n\n".join(parts)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Policy radar"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent policy radar (%d schemes) to %s", len(rows), req.sender)


def _send_holdco_screen(req: EmailRequest) -> None:
    """Holdco-discount screen → listed holders whose stake NAV exceeds their own market cap."""
    log.info("running holdco screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning for holding companies trading below the value of their "
                     "listed stakes (the Elcid pattern). ~1 min; the ranked list will land here.")
    con = connect()
    try:
        rows = _screen_run(lambda: holdco.holdco_discounts(con, limit=config.HOLDCO_LIMIT))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The holdco scan timed out — please resend `screen: holdco` in a moment.")
        return
    if not rows:
        _reply_text(req, "No holdcos surfaced yet — this needs SHP ingested across the universe. "
                         "Run the one-time `backfill_universe.py` (Nifty-500 + known holdcos) to seed it.")
        return
    tbl_rows = []
    for i, r in enumerate(rows, 1):
        disc = f"{r['discount_pct']:+.0f}%" if r["discount_pct"] is not None else "n/a"
        top = ", ".join(f"{inv} {pct:.1f}%" for inv, _nm, pct, _v in r["top_stakes"][:3])
        tbl_rows.append([i, r["holder"], disc, _crore(r["own_mcap_cr"]),
                         _crore(r["stake_nav_cr"]), top])
    table = _md_table(
        ["#", "Holdco", "Discount", "Own mcap", "Stake NAV", "Top listed stakes"],
        tbl_rows, align="rlrrrl")
    md = ("**🏦 Holdco discounts — listed stake NAV vs own market cap** (the Elcid trade, generalised)\n\n"
          "**Reply with a number for that holding company's full deep report.**\n\n"
          + table + "\n\n"
          "_Discount = 1 − own market cap ÷ stake NAV. Counts only **disclosed listed** stakes "
          "(SHP promoter + public >1% tables); unlisted subsidiaries aren't valued. Coverage grows "
          f"with SHP ingested. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["holder"], r["holder_name"]) for r in rows]
    _set_pending(req, "screen:holdco", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — holdco"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent holdco screen (%d names) to %s", len(rows), req.sender)


_INVESTOR_CAVEAT = ("_Tracks the SHP public/promoter tables — only holders **disclosed by "
                    "name** are visible (public stakes below ~1% aren't filed at all), and "
                    "coverage is bounded by the SHP universe ingested so far. A drop out of the "
                    "list can mean a full exit **or** trimming below the ~1% disclosure floor._")


def _send_investor_screen(req: EmailRequest) -> None:
    """What every tracked marquee investor did last quarter (entered/exited/added/trimmed),
    across the SHP data → numbered list of the stocks they moved; reply → deep report."""
    log.info("running investor-moves screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — checking what the tracked marquee investors did last quarter "
                     "across the shareholding data. Lands in this thread shortly.")
    con = connect()
    try:
        moves = _screen_run(lambda: investors.all_moves(con))
    finally:
        con.close()
    if moves is None:
        _reply_text(req, "The investor scan timed out — please resend `screen: investors` shortly.")
        return
    if not moves:
        _reply_text(req, "No tracked investor showed a disclosed move ≥0.5pp last quarter across "
                         "the ingested SHP universe.\n\n" + _INVESTOR_CAVEAT)
        return
    sym_arrow = {"entered": "🟢 new", "exited": "🔴 exit", "added": "➕ add", "trimmed": "➖ trim"}
    tbl_rows, cands, n = [], [], 0
    for canon in investors.roster():
        m = moves.get(canon)
        if not m:
            continue
        for kind in ("entered", "added", "trimmed", "exited"):
            for r in m[kind]:
                n += 1
                delta = (f"{r['delta']:+.2f}pp" if "delta" in r
                         else (f"{r['pct']:.2f}%" if kind == "entered" else f"was {r['prev_pct']:.2f}%"))
                tbl_rows.append([n, canon, sym_arrow[kind], r["symbol"], delta, r["name"][:28]])
                cands.append(_MenuItem(r["symbol"], r["name"]))
    table = _md_table(["#", "Investor", "Move", "Symbol", "Δ / stake", "Company"],
                      tbl_rows, align="rllrrl")
    md = (f"**👤 Marquee-investor moves — last disclosed quarter** ({len(moves)} of "
          f"{len(investors.roster())} tracked names moved)\n\n"
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n" + _INVESTOR_CAVEAT + f"\n\n_(Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:investors", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — investors"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent investor-moves screen (%d moves, %d investors) to %s", n, len(moves), req.sender)


def _send_investor(name: str, req: EmailRequest) -> None:
    """One marquee investor's current book + last-quarter moves → numbered holdings;
    reply → deep report on any of them."""
    canon = investors.resolve(name)
    if not canon:
        roster = ", ".join(investors.roster()[:8])
        _reply_text(req, f"'{name}' isn't a tracked investor. Try one of: {roster}… "
                         "or `screen: investors` for everyone's latest moves.")
        return
    log.info("running investor book for %s (req from %s)", canon, req.sender)
    _reply_text(req, f"📩 Got it — pulling **{canon}**'s disclosed holdings and last-quarter "
                     "moves from the shareholding data.")
    con = connect()
    try:
        book = _screen_run(lambda: investors.holdings(con, canon))
        mv = _screen_run(lambda: investors.moves(con, canon))
    finally:
        con.close()
    if not book:
        _reply_text(req, f"No disclosed (≥~1%) holdings found for **{canon}** in the ingested "
                         "SHP universe yet.\n\n" + _INVESTOR_CAVEAT)
        return
    def _cost_cell(r):
        if r.get("gain_pct") is not None:
            avg = r.get("cost_avg")
            return f"{r['cost_emoji']} {r['gain_pct']:+.0f}%" + (f" (~₹{avg:,.0f})" if avg else "")
        if r.get("zone_lo") is not None:              # held since before our data
            return "⚪ pre-data"
        return "—"
    table = _md_table(["#", "Symbol", "Company", "Stake", "Now vs their cost", "As of"],
                      [[i, r["symbol"], r["name"], f"{r['pct']:.2f}%", _cost_cell(r),
                        f"{r['as_of']:%b-%Y}"] for i, r in enumerate(book, 1)], align="rlllrr")
    parts = [f"**👤 {canon} — disclosed holdings** ({len(book)} names ≥~1%)\n\n"
             "**Reply with a number for that stock's full deep report.**\n\n" + table
             + "\n\n_**Now vs their cost** = current price vs the price zone of the quarters "
             "**this investor** added in (inferred — exact prices aren't disclosed). 🟢 near cost · "
             "🟡 in profit · 🟠🔴 large gains → watch for profit-booking; ⚪ pre-data = held since "
             "before our data (cost unknown). Deepens as more shareholding history is ingested._"]
    if mv and (mv["entered"] or mv["exited"] or mv["added"] or mv["trimmed"]):
        def _fmt(items, kind):
            return ", ".join(
                (f"{r['symbol']} ({r['delta']:+.2f}pp)" if "delta" in r
                 else (f"{r['symbol']} ({r['pct']:.2f}%)" if kind == "entered"
                       else f"{r['symbol']} (was {r['prev_pct']:.2f}%)")) for r in items)
        mv_lines = ["", "**Last-quarter moves:**"]
        if mv["entered"]:
            mv_lines.append(f"- 🟢 **New:** {_fmt(mv['entered'], 'entered')}")
        if mv["added"]:
            mv_lines.append(f"- ➕ **Added:** {_fmt(mv['added'], 'added')}")
        if mv["trimmed"]:
            mv_lines.append(f"- ➖ **Trimmed:** {_fmt(mv['trimmed'], 'trimmed')}")
        if mv["exited"]:
            mv_lines.append(f"- 🔴 **Exited / below disclosure:** {_fmt(mv['exited'], 'exited')}")
        parts.append("\n".join(mv_lines))
    parts.append("\n" + _INVESTOR_CAVEAT + f"\n\n_(Reply within {PENDING_TTL_H}h.)_")
    md = "\n\n".join(parts)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in book]
    _set_pending(req, f"investor:{canon}", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, f"{canon} — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent investor book for %s (%d holdings) to %s", canon, len(book), req.sender)
