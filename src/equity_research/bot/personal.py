"""Your own book: the sell ranking and raise-₹X plan, Thesis Guard, Reality Check, the scorecard and filing alerts."""

from __future__ import annotations

import os
import threading
from datetime import date, datetime

from equity_research import config, scan, schedule
from equity_research.analysis import (
    keyword_alerts,
    reality_check,
    sell_advisor,
    thesis_guard,
    track_record,
)
from equity_research.bot.core import (
    ALLOWED,
    IST,
    PENDING_TTL_H,
    SCAN_HOUR,
    _inr,
    _md_table,
    _MenuItem,
    _needs_llm,
    _re_subject,
    _reply_text,
    _screen_run,
    _set_pending,
    log,
)
from equity_research.common import llm
from equity_research.common.db import connect
from equity_research.portfolio import tax
from equity_research.reports import email as emailer
from equity_research.reports import (
    reality_brief,
    scorecard_brief,
    thesis_brief,
)
from equity_research.reports.inbox import EmailRequest


def _send_thesis(req: EmailRequest, action: str, company: str, text: str) -> None:
    con = connect()
    try:
        if action == "list":
            items = [(t, thesis_guard.check(con, t)) for t in thesis_guard.load(con)]
            md = thesis_brief.render_list(items)
        else:
            hit = reality_check._resolve(con, company)
            if not hit:
                md = (f"🛡️ Couldn't pin **{company}** to one NSE listing — send it again with the exact name "
                      "or NSE symbol (e.g. `thesis: BEL — …`).")
            elif action == "remove":
                md = (f"🛡️ Stopped tracking the thesis on **{hit[1]}**." if thesis_guard.remove(con, hit[0])
                      else f"🛡️ There was no thesis on **{hit[1]}**.")
            elif action == "show":
                ts = thesis_guard.load(con, hit[0])
                if not ts:
                    md = (f"🛡️ No thesis on **{hit[1]}** yet — write one: `thesis: {hit[0]} — <why you own it>; "
                          "<your rules>`.")
                else:
                    res = thesis_guard.check(con, ts[0], with_judge=llm.configured())
                    thesis_guard.record(con, hit[0], res)
                    md = thesis_brief.render_one(ts[0], res)
            else:                                            # set
                if _needs_llm(req, "Thesis Guard"):
                    return
                _reply_text(req, f"🛡️ Got it — turning your reasons for **{hit[1]}** into checks and running "
                                 "them now (~1 min).")
                t, unclear = thesis_guard.create(con, hit[0], hit[1], text)
                if not t:
                    md = (f"🛡️ Couldn't turn that into checks ({unclear}). Try naming concrete reasons — "
                          "growth, margins, debt, promoter or institutional stakes, a product or orders — "
                          "and rules like 'exit below 250'.")
                else:
                    res = thesis_guard.check(con, t)
                    thesis_guard.record(con, hit[0], res)
                    note = f"_Couldn't make a check out of: {unclear}_" if unclear else ""
                    md = thesis_brief.render_one(t, res, note=note)
    finally:
        con.close()
    emailer.send_report(_re_subject(req.subject), md, to=req.sender, html=emailer.body_html(md, "Thesis Guard"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent thesis guard (%s %s) to %s", action, company, req.sender)


_thesis_lock = threading.Lock()


def _thesis_sweep_worker() -> None:
    """Re-check every thesis; email only what changed. One sweep per trading day (marked at the end)."""
    if not _thesis_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        moved = []
        for t in thesis_guard.load(con):
            try:
                res = thesis_guard.check(con, t, with_judge=llm.configured())
            except Exception:  # noqa: BLE001
                log.exception("thesis guard: check failed for %s", t["symbol"])
                continue
            ch = thesis_guard.changes(t["last"], res)
            thesis_guard.record(con, t["symbol"], res)
            if ch:
                moved.append((t, res, ch))
        scan._set_meta(con, "last_thesis_sweep", datetime.now(IST).date().isoformat())
        to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
        if moved and to:
            md = thesis_brief.render_changes(moved)
            emailer.send_report(f"🛡️ Thesis Guard — {len(moved)} change(s) tonight", md, to=to,
                                html=emailer.body_html(md, "Thesis Guard"))
        log.info("thesis guard sweep: %d thesis(es) changed", len(moved))
    except Exception:  # noqa: BLE001
        log.exception("thesis guard sweep failed")
    finally:
        con.close()
        _thesis_lock.release()


def maybe_thesis_sweep() -> None:
    """Heartbeat hook: once per trading day, after the evening scan hour, in a background thread."""
    now = datetime.now(IST)
    if now.hour < SCAN_HOUR or _thesis_lock.locked():
        return
    con = connect()
    try:
        done = scan._meta(con, "last_thesis_sweep") == now.date().isoformat()
        has = con.execute("SELECT count(*) FROM theses WHERE active").fetchone()[0]
    finally:
        con.close()
    if done or not has or not scan.market_open_today():
        return
    threading.Thread(target=_thesis_sweep_worker, name="thesis-sweep", daemon=True).start()


def _send_reality_check(req: EmailRequest, raw: str) -> None:
    if _needs_llm(req, "Reality Check"):
        return
    _reply_text(req, "🔍 Got it — reading it, pulling each claim out and checking it against the "
                     "company's exchange filings and reported numbers (~1–3 min). The verdict lands here.")
    con = connect()
    try:
        res = _screen_run(lambda: reality_check.run(con, raw), timeout=config.REALITY_TIMEOUT_S)
        if res is None:
            _reply_text(req, "The check timed out this time — please resend it shortly.")
            return
        if not res.extracted:
            why = res.post.note or "no stock claims could be read from it"
            _reply_text(req, f"🔍 Couldn't check that — {why}. You can paste the post's text instead: "
                             "`reality check: <the text>`.")
            return
        rep = reality_brief.build(res)
        if rep["picks"]:
            _set_pending(req, "reality", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
        _track_tip(con, res, req.subject)
    finally:
        con.close()
    emailer.send_report(_re_subject(req.subject), rep["markdown"], to=req.sender,
                        html=emailer.body_html(rep["markdown"], "Reality Check"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent reality check (%s) to %s", res.verdict[0], req.sender)


def _track_tip(con, res, subject: str) -> None:
    """Log the tip itself (not our call) so the scorecard can show how checked tips played out:
    each company the post was about, long if the post was bullish, avoid if bearish."""
    direction = (res.extracted or {}).get("direction")
    stance = {"bullish": "long", "bearish": "avoid"}.get(direction)
    if not stance or not track_record.enabled():
        return
    for co in res.companies.values():
        try:
            track_record.log_call(con, "tip", co.symbol, stance, "TIP " + ("↑" if stance == "long" else "↓"),
                                  context=f"{res.verdict[0]} — {(res.extracted or {}).get('summary', '')}",
                                  ref=res.post.url or subject)
        except Exception:  # noqa: BLE001
            log.exception("track record: couldn't log the tip on %s", co.symbol)


def _send_scorecard(req: EmailRequest) -> None:
    con = connect()
    try:
        md = scorecard_brief.build_scorecard(con)
    finally:
        con.close()
    emailer.send_report(_re_subject(req.subject), md, to=req.sender, html=emailer.body_html(md, "Scorecard"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent scorecard to %s", req.sender)


def maybe_scorecard() -> None:
    """Weekly 📊 Scorecard push (Saturday ≥18:00 IST, with the other weekly pushes) — only once
    there's at least one call logged, so a fresh install doesn't mail an empty table."""
    if not track_record.enabled() or not scan.weekly_due("last_scorecard_week"):
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        return
    con = connect()
    try:
        if not con.execute("SELECT count(*) FROM calls").fetchone()[0]:
            scan.mark_weekly("last_scorecard_week", con)
            return
        md = scorecard_brief.build_scorecard(con)
    except Exception:  # noqa: BLE001
        log.exception("scorecard build failed")
        return
    finally:
        con.close()
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"📊 Scorecard — {today}{schedule.catch_up_note(schedule.open_slot())}", md, to=to,
                        html=emailer.body_html(md, "Scorecard"))
    scan.mark_weekly("last_scorecard_week")
    log.info("weekly scorecard sent to %s", to)


def _handle_alert(req: EmailRequest, action: str, keyword: str) -> None:
    """🔔 Filing-alert management — add / list / remove / clear the user's keyword alerts."""
    con = connect()
    try:
        if action == "add":
            kw = keyword_alerts.add_keyword(con, keyword)
            kws = keyword_alerts.list_keywords(con)
            _reply_text(req, f"🔔 Alert set for **{kw}** — I'll email you when any company files an "
                             "announcement that matches.\n\nWatching now: "
                             f"{', '.join(kws)}\n\n_Forward-looking (only new filings from now). Matches "
                             "the words you type and their longer forms (‘order’ also catches ‘orders’) — "
                             "use root words. `alerts` to list · `unalert: <kw>` to remove._")
        elif action == "remove":
            existed = keyword_alerts.remove_keyword(con, keyword)
            kws = keyword_alerts.list_keywords(con)
            tail = f" Still watching: {', '.join(kws)}." if kws else " No alerts set now."
            _reply_text(req, (f"🔕 Removed the alert for **{keyword.strip().lower()}**." if existed
                              else f"No alert for '{keyword.strip()}' was set.") + tail)
        elif action == "clear":
            n = keyword_alerts.clear_keywords(con)
            _reply_text(req, f"🔕 Cleared {n} announcement alert(s).")
        else:                                          # list
            kws = keyword_alerts.list_keywords(con)
            if kws:
                _reply_text(req, "🔔 **Your announcement alerts**\n\n" + "\n".join(f"- {k}" for k in kws)
                                 + "\n\n_Add `alert: <keyword>` · remove `unalert: <keyword>`._")
            else:
                _reply_text(req, "You have no announcement alerts set. Add one with `alert: <keyword>` — e.g. "
                                 "`alert: order win`, `alert: QIP`, `alert: capacity expansion`.")
    finally:
        con.close()
    log.info("filing-alert %s: %s", action, keyword or "(all)")


def _push_alerts(matches: list[dict]) -> None:
    """Send ONE email with the new keyword-matched filings (no reply-thread — a standalone push)."""
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot push announcement alerts")
        return
    tbl = [[i, m["symbol"], m["keyword"], m["headline"][:70], m["an_dt"]]
           for i, m in enumerate(matches, 1)]
    table = _md_table(["#", "Symbol", "Keyword", "Headline", "Filed"], tbl, align="rllll")
    body = (f"**🔔 Announcements — {len(matches)} match{'es' if len(matches) != 1 else ''}**\n\n"
            "New exchange filings matching your saved keywords:\n\n" + table)
    links = "\n".join(f"- **{m['symbol']}** [{m['keyword']}] — {m['url']}"
                      for m in matches if m.get("url"))
    if links:
        body += "\n\n**Documents:**\n" + links
    body += "\n\n_Manage with `alerts` · `alert: <kw>` · `unalert: <kw>`._"
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"🔔 Announcements — {today}", body, to=to,
                        html=emailer.body_html(body, "Announcements"))
    log.info("pushed %d announcement alert(s) to %s", len(matches), to)


_SELL_LEGEND = (
    "\n\n---\n\n**📖 What the terms mean** (plain English — skip if you know them)\n\n"
    "- **Keep score (0-100):** how strong a *hold* each stock is — higher = keep, lower = sell first. "
    "It blends the five things below, each judged **only against your own holdings** (so it ranks *your* book).\n"
    "- **DCF upside / overvalued:** our estimate of what the business is worth vs its current price. "
    "'~20% upside' = looks about 20% cheap; '~90% overvalued' = the price is far above what it seems worth.\n"
    "- **Cheaper than X% of own history:** where today's valuation sits vs how expensive/cheap *this* stock "
    "usually is. 'Cheaper than 75% of its own history' = unusually cheap for it.\n"
    "- **Piotroski (quality):** a 0–9 financial-health score (profit, debt, efficiency). 8–9 strong, 0–2 weak.\n"
    "- **Forensic (0–4):** accounting-safety score — higher = cleaner books (no distress / manipulation / pledge flags).\n"
    "- **Momentum vs Nifty:** how the price did against the market over ~3 months. 'lags −15%' = 15% worse than the index.\n"
    "- **Smart-money flow:** whether big institutions (mutual funds, insurers, foreign funds) added or trimmed last "
    "quarter. 'trimming' = they're reducing their stake.\n"
    "- **Verdict:** 🔴 sell candidate (weakest of your book) · 🟡 trim if needed · 🟢 keep (strongest) · "
    "⚪ no data (not analysed yet — email the symbol once)."
)


def _send_sell_advisor(req: EmailRequest) -> None:
    """Sell-priority ranking of the user's holdings (Version A — merit only, no cost/tax yet):
    weakest hand first, so if you need cash you sell from the top down. Reply a number → that
    holding's full deep report before acting."""
    log.info("running sell advisor (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — ranking your holdings by **which to sell first** on merit "
                     "(valuation headroom, quality, forensic, momentum, smart-money). "
                     "~1–2 min; the ranked list lands in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: sell_advisor.sell_ranking(con))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The sell ranking timed out this time — please resend `sell` in a moment.")
        return
    if not rows:
        _reply_text(req, "No holdings tagged yet — add stocks to your watchlist as 'holding' first, "
                         "then resend `sell`.")
        return
    con = connect()
    try:
        book = sell_advisor.book_summary(con)
    finally:
        con.close()
    if book:
        def _pl(sym: str) -> list:
            b = book.get(sym)
            if not b:
                return ["—", "—", "—"]
            terms = " + ".join(sorted({"short": "<1 yr", "long": ">1 yr", "undated": "no date"}[t] for t in b["terms"]))
            return [_inr(b["value"]), f"{_inr(b['pnl'])} ({b['pnl_pct']:+.0f}%)", terms]
        table = _md_table(
            ["#", "Symbol", "Company", "Keep", "Verdict", "Value", "Profit / loss", "Held", "Why"],
            [[i, r["symbol"], r["name"][:24],
              (f"{r['keep_score']:.0f}" if r["keep_score"] is not None else "—"), r["verdict"],
              *_pl(r["symbol"]), r["why"]]
             for i, r in enumerate(rows, 1)],
            align="rllllrrll")
    else:
        table = _md_table(
            ["#", "Symbol", "Company", "Keep", "Verdict", "Why"],
            [[i, r["symbol"], r["name"][:24],
              (f"{r['keep_score']:.0f}" if r["keep_score"] is not None else "—"),
              r["verdict"], r["why"]]
             for i, r in enumerate(rows, 1)],
            align="rlllll")
    md = ("**💰 Which to sell first — your holdings, ranked**\n\n"
          "If you need cash, sell from the **top** (weakest hand) down. **Keep score 0-100** "
          "(higher = stronger hold): 35% valuation headroom (DCF upside + cheap-vs-own-history) · "
          "25% quality (Piotroski) · 20% forensic · 10% momentum vs Nifty · 10% smart-money flow — "
          "each ranked **within your own book**. "
          "**Reply with a number for that holding's full deep report before you act.**\n\n"
          + table + "\n\n"
          + ("**Need a set amount?** Send `raise 50000` (or `take out 2 lakh`) — exactly what to sell, "
             "with the tax.\n\n" if book else
             "_Add quantities and buy prices in the web UI's **💼 My holdings** (or `holdings.csv`) to see "
             "value, profit / loss and tax here — and to ask `raise 50000` for exactly what to sell._\n\n")
          + f"_Decision support; the call is yours. (Reply within {PENDING_TTL_H}h.)_"
          + _SELL_LEGEND)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "sell", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Sell-priority — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sell advisor (%d holdings) to %s", len(rows), req.sender)


_TERM = {"short": "< 1 yr (short-term)", "long": "> 1 yr (long-term)", "unknown": "no date", "mixed": "mixed"}


def _plan_md(title: str, plan: dict, *, keep_col: bool) -> str:
    heads = ["#", "Stock", "Sell", "≈ You get", "Gain / loss", "Held"] + (["Keep"] if keep_col else [])
    rows = []
    for i, r in enumerate(plan["rows"], 1):
        rows.append([i, f"**{r['symbol']}** {r['name'][:22]}",
                     f"{r['shares']:,} of {r['held']:,}" + (" (all)" if r["shares"] == r["held"] else ""),
                     _inr(r["proceeds"]), _inr(r["gain"]), _TERM[r["term"]]]
                    + ([f"{r['keep']:.0f}" if r["keep"] is not None else "—"] if keep_col else []))
    t = plan["tax"]
    lines = [f"### {title}", _md_table(heads, rows, align="rllrrl" + ("r" if keep_col else ""))]
    tax_bits = []
    if t["st_gain"]:
        tax_bits.append(f"short-term gain {_inr(t['st_gain'])}")
    if t["lt_gain"]:
        tax_bits.append(f"long-term gain {_inr(t['lt_gain'])}"
                        + (f" ({_inr(t['exemption_used'])} of it tax-free)" if t["exemption_used"] else ""))
    lines.append(f"**≈ {_inr(plan['proceeds'])} from the sale · tax ≈ {_inr(t['tax'])} · in hand ≈ "
                 f"{_inr(plan['net'])}**" + (f"  \n_{' · '.join(tax_bits)}_" if tax_bits else ""))
    if plan["has_unknown"]:
        lines.append(f"_Includes shares with no buy date — {_inr(t['unknown_gain'])} of gain / loss whose tax "
                     "isn't counted above (add the date in 💼 My holdings)._")
    return "\n\n".join(lines)


def _send_raise_plan(req: EmailRequest, amount: float) -> None:
    """'raise ₹X' — what to sell: 🧾 least tax vs 💪 weakest first, with shares, money and tax."""
    log.info("running raise plan for %s (req from %s)", amount, req.sender)
    con = connect()
    try:
        has_lots = con.execute("SELECT count(*) FROM holding_lots").fetchone()[0] > 0
    finally:
        con.close()
    if not has_lots:
        _reply_text(req, f"💰 To plan how to raise **{_inr(amount)}** I need your quantities and buy prices — add "
                         "them in the web UI's **💼 My holdings** (your watchlist stocks are already listed there; or "
                         "`holdings.csv`). Then resend.\n\n**How to enter:** with a buy date, the quantity and "
                         "price **as you bought them** — splits / bonuses since are applied for you (e.g. "
                         "100 @ ₹500 bought before a 1:5 split shows as 500 @ ₹100); without a date, what your "
                         "broker shows **today**. The date adds the tax estimate.")
        return
    _reply_text(req, f"📩 Got it — working out what to sell to raise **{_inr(amount)}** (least tax vs weakest "
                     "holdings first). ~1–2 min.")
    con = connect()
    try:
        ranking = _screen_run(lambda: sell_advisor.sell_ranking(con)) or []
        res = sell_advisor.raise_plan(con, amount, ranking)
    finally:
        con.close()
    plans = res["plans"]
    if not plans:
        _reply_text(req, "None of your holdings with quantities has a recent price yet — try again after "
                         "the evening data refresh.")
        return
    tax_p, merit_p = plans["tax"], plans["merit"]
    parts = [f"# 💰 Raise {_inr(amount)} — what to sell",
             f"Your holdings with quantities are worth **{_inr(res['book_value'])}** at the last close."]
    if tax_p["short_by"] > 0:
        parts.append(f"⚠️ That's more than they're worth — selling **everything** raises ≈ "
                     f"{_inr(tax_p['proceeds'])}, {_inr(tax_p['short_by'])} short.")
    if res["same"] or not ranking:
        parts.append(_plan_md("🧾 The plan — least tax" + ("" if ranking else " (keep scores unavailable this "
                                                                          "time)"), tax_p, keep_col=bool(ranking)))
        if ranking:
            parts.append("_Selling your weakest holdings first lands on the same sale — no trade-off here._")
    else:
        saving = merit_p["tax"]["tax"] - tax_p["tax"]["tax"]
        parts.append(_plan_md("🧾 Plan 1 — least tax", tax_p, keep_col=True))
        parts.append(_plan_md("💪 Plan 2 — sell your weakest holdings first (lowest keep score)", merit_p,
                              keep_col=True))
        if saving > 1:
            parts.append(f"**The trade-off:** Plan 1 saves ≈ **{_inr(saving)}** in tax; Plan 2 lets go of the "
                         "holdings you'd least regret selling on merit. Small saving → Plan 2 is usually the "
                         "better hold-quality call; big saving → Plan 1.")
        else:
            parts.append("**The trade-off:** the tax is about the same either way, so Plan 2 (weakest first) "
                         "costs you nothing extra.")
    tips = {(t["symbol"], t["days"]): t for p in plans.values() for t in p["tips"]}.values()
    if tips:
        parts.append("### ⏳ Worth waiting?\n\n" + "\n".join(
            f"- **{t['symbol']}** — some of these shares turn long-term in **{t['days']} days**; selling "
            f"them then instead of now saves ≈ {_inr(t['save'])} in tax (more if the yearly ₹1.25 lakh "
            "long-term exemption covers it)." for t in sorted(tips, key=lambda t: -t["save"])))
    if res.get("booked"):
        bk = res["booked"]
        parts.append(f"_Already booked this year (sells you recorded): long-term {_inr(bk['lt_gain'])}, short-term "
                     f"{_inr(bk['st_gain'])} — counted: the tax above is only what these sales would add._")
    if res["no_qty"]:
        parts.append("_Not included (no quantity yet): " + ", ".join(res["no_qty"]) + " — add them in "
                     "💼 My holdings._")
    parts.append(
        "---\n_How this is worked out: prices are the **last close** (the real sale price will differ); shares "
        "go **oldest first** (FIFO — how Indian demat sales are taxed); tax is an **estimate** at "
        f"{config.STCG_RATE:.1%} short-term / {config.LTCG_RATE:.1%} long-term + {config.TAX_CESS:.0%} cess, "
        f"with ₹{config.LTCG_EXEMPTION:,.0f} of long-term gain tax-free a year, losses set off, and the gains "
        "from **sells you've recorded this year** counted (others aren't known). Bonus shares cost ₹0 and are dated "
        "on allotment; shares held since 31-Jan-2018 use that day's price as cost (grandfathering). Brokerage, STT "
        "and surcharge are left out. Keep score = the `sell` ranking's merit score (higher = stronger hold). Buys "
        "with a date are counted as entered (as bought) and brought through splits / bonuses since; buys without "
        f"one are taken as today's numbers. Long-term gains on listed shares: {tax.ltcg_section(date.today())}. "
        "Decision support, not tax advice — check with your CA for large sales._")
    md = "\n\n".join(parts)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Raise cash — what to sell"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent raise plan (%s) to %s", amount, req.sender)


_alert_lock = threading.Lock()


def _alert_scan_worker() -> None:
    """Background pass: match new market-wide filings against saved keywords and push any hits.
    `scan_new` no-ops cheaply when no keywords are set (before any sweep). Holds `_alert_lock`."""
    if not _alert_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        matches = keyword_alerts.scan_new(con)
        scan.mark_alert_scan(con)
        if matches:
            _push_alerts(matches)
    except Exception:  # noqa: BLE001 — never let the alert scan crash the bot
        log.exception("keyword-alert scan failed")
    finally:
        con.close()
        _alert_lock.release()


def maybe_alert_scan() -> None:
    """Heartbeat hook: run the keyword-alert sweep at most ~every 20 min (08:00–23:00 IST), in a
    background thread. Cheap no-op when the user has no alerts set."""
    if _alert_lock.locked() or not scan.alert_scan_due():
        return
    threading.Thread(target=_alert_scan_worker, name="alert-scan", daemon=True).start()
