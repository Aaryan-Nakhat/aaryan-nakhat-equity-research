"""Sell-priority advisor — of *your* holdings, which to sell first if you need cash.

**Merit (Version A).** Ranks the stocks you own purely on forward-looking merit —
which you'd *least regret* parting with — reusing the deterministic analysis layer
(``screener`` / ``quant`` / ``technical`` / ``ownership``).

**Cost, tax and sizing (Version B, below).** Once holdings carry quantities and buy prices
(💼 My holdings; the buy date optional), ``raise_plan`` works out what to sell to raise ₹X —
least tax vs weakest-first — and ``book_summary`` adds value / P&L to the plain ranking.

Each holding gets a **keep score** (0-100). Every signal is rank-normalised **within your
own book**, so the ranking answers exactly the question asked — *of the stocks I hold,
which is the weakest hand* — not "how does this rank against the Nifty-500". Signals and
weights (``_WEIGHTS``):

- **Valuation headroom** (upside 0.20 + cheapness 0.15) — DCF margin of safety (median
  intrinsic vs price) plus cheap-vs-own-history percentile. Little upside / richly valued
  ⇒ sell first (you forgo the least future return).
- **Quality** (0.25) — Piotroski F (0-9).
- **Forensic** (0.20) — Altman Z · Beneish M · Sloan accruals · no promoter pledge (0-4).
- **Momentum** (0.10) — 3-month relative strength vs Nifty. A laggard is easier to let go.
- **Smart-money flow** (0.10) — net institutional QoQ accumulation; institutions exiting
  ⇒ sell first.

Sell-first = **lowest keep score**; the list is returned worst-first. Names bucket into
🔴 Sell candidates / 🟡 Trim if needed / 🟢 Keep. This is decision *support* — the final
call is yours; reply a number to pull that stock's full deep report before you act.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import duckdb

from equity_research import watchlist
from equity_research.analysis import ownership, quant, screener, technical, valuation
from equity_research.portfolio import tax
from equity_research.portfolio.tax import tax_estimate  # re-exported: the email layer uses it

log = logging.getLogger(__name__)

# Weights sum to 1.0. Valuation gets the most weight: when you sell, the stock with the
# least upside left is the one whose sale costs you the least going forward.
_WEIGHTS = {"upside": 0.20, "cheapness": 0.15, "quality": 0.25,
            "forensic": 0.20, "momentum": 0.10, "inst_flow": 0.10}
_SIGNALS = list(_WEIGHTS)

# Institutional holder categories whose QoQ move counts as "smart money".
_INST_CATS = {"mutual fund", "insurance company", "FPI", "bank / FI"}


def _upside_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """DCF upside relative to **price**: (median intrinsic − price)/price × 100. Positive =
    undervalued (that much upside to fair value), negative = overvalued (floored at −100%).
    Measured against price — not the DCF median — so it reads as a sane move-to-fair-value
    (dividing by a tiny median otherwise yields absurd four-figure %). ``None`` when the DCF
    isn't usable (e.g. banks/financials) — the cheapness signal then carries valuation."""
    try:
        inp = quant.dcf_inputs(con, symbol)
        if not inp.usable:
            return None
        res = quant.monte_carlo_dcf(inp)
    except Exception:  # noqa: BLE001 — a thin/odd name must not break the ranking
        return None
    if res.median is None or not res.price:
        return None
    return (res.median - res.price) / res.price * 100


def _momentum_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """3-month out/under-performance vs Nifty, in %. ``relative_strength`` returns a *ratio*
    (>1 = outperform), so convert to a percentage: (ratio − 1) × 100. Higher = leading."""
    try:
        rs = technical.relative_strength(con, symbol)
    except Exception:  # noqa: BLE001
        return None
    return None if rs is None else (rs - 1) * 100


def _inst_flow_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """Net institutional accumulation (pp) over the latest QoQ shareholding diff: MF /
    insurer / FPI / bank entering + adding, minus exiting + trimming. Positive = smart
    money moving in. ``None`` when there aren't two quarters to diff."""
    try:
        ch = ownership.ownership_changes(con, symbol)
    except Exception:  # noqa: BLE001
        return None
    if not ch:
        return None
    flow = 0.0
    for r in ch["entered"]:
        if r["category"] in _INST_CATS:
            flow += r.get("pct") or 0.0
    for r in ch["added"]:
        if r["category"] in _INST_CATS:
            flow += r.get("delta") or 0.0
    for r in ch["exited"]:
        if r["category"] in _INST_CATS:
            flow -= r.get("prev_pct") or 0.0
    for r in ch["trimmed"]:
        if r["category"] in _INST_CATS:
            flow -= abs(r.get("delta") or 0.0)
    return flow


def _why(r: dict) -> str:
    """Short, sell-relevant reasoning — lead with valuation, then the weakest signals."""
    bits: list[str] = []
    u = r.get("upside")
    if u is not None:
        # cap the shown magnitude — a DCF median far above price yields silly 3-figure %
        # that reads as noise (the ranking still uses the true value).
        lead = ">" if abs(u) > 200 else "~"
        bits.append(f"DCF {lead}{min(abs(u), 200):.0f}% {'upside' if u >= 0 else 'overvalued'}")
    elif r.get("cheapness") is not None:
        ch = r["cheapness"]
        bits.append(f"{'cheaper than' if ch >= 50 else 'pricier than'} "
                    f"{(ch if ch >= 50 else 100 - ch):.0f}% of own history")
    if r.get("quality") is not None:
        bits.append(f"Piotroski {r['quality']:.0f}/9")
    if r.get("forensic") is not None and r["forensic"] < 3:
        bits.append(f"forensic {r['forensic']:.1f}/4")
    m = r.get("momentum")
    if m is not None:
        bits.append(f"{'leads' if m >= 0 else 'lags'} Nifty {m:+.0f}% (3m)")
    f = r.get("inst_flow")
    if f is not None and abs(f) >= 0.1:
        bits.append(f"institutions {'adding' if f > 0 else 'trimming'} {f:+.1f}pp")
    return " · ".join(bits)


def _verdict(rank: int, n: int) -> str:
    """Bucket by position in your own book: weakest third sell, middle trim, top keep."""
    if n <= 2:
        return "🔴 Sell first" if rank == 1 else "🟢 Keep"
    third = n / 3
    if rank <= third:
        return "🔴 Sell candidate"
    if rank <= 2 * third:
        return "🟡 Trim if needed"
    return "🟢 Keep"


def sell_ranking(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Rank the user's *holdings* sell-first on merit (Version A — no cost/tax yet).

    Returns ``[{symbol, name, keep_score, verdict, upside, cheapness, quality, forensic,
    momentum, inst_flow, pe, pb, why}, …]``, **weakest hand first** (sell candidates on
    top). Empty if no holdings are tagged. Best-effort: a holding missing a signal is
    scored on the rest (a missing signal normalises to the neutral middle)."""
    holdings = watchlist.entries_by_type(con, "holding")
    rows: list[dict] = []
    for sym, name in holdings:
        try:
            snap = valuation.snapshot(con, sym) or {}
            rows.append({
                "symbol": sym, "name": name or sym,
                "upside": _upside_raw(con, sym),
                "cheapness": screener._cheapness_raw(con, sym),
                "quality": screener._quality_raw(con, sym),
                "forensic": screener._forensic_raw(con, sym),
                "momentum": _momentum_raw(con, sym),
                "inst_flow": _inst_flow_raw(con, sym),
                "pe": snap.get("pe_ttm"), "pb": snap.get("pb"),
            })
        except Exception:  # noqa: BLE001 — one bad holding must not sink the whole ranking
            log.exception("sell scoring failed for %s", sym)
            continue
    if not rows:
        return []

    # Split off holdings with no usable signal at all (not yet ingested) — they can't be
    # ranked and would otherwise sit at a meaningless mid-pack 50, mislabelled "trim".
    scored = [r for r in rows if any(r[k] is not None for k in _SIGNALS)]
    unscored = [r for r in rows if r not in scored]

    for key in _SIGNALS:
        screener._normalise(scored, key)
    for r in scored:
        r["keep_score"] = round(100 * sum(_WEIGHTS[k] * r[k + "_n"] for k in _SIGNALS), 1)
    # keep_score asc → weakest hand first; symbol asc as a stable tie-break so the order
    # (and any future week-over-week deltas) doesn't jitter run-to-run.
    scored.sort(key=lambda r: (r["keep_score"], r["symbol"]))
    n = len(scored)
    for i, r in enumerate(scored, 1):
        r["verdict"] = _verdict(i, n)
        r["why"] = _why(r)
    for r in sorted(unscored, key=lambda r: r["symbol"]):
        r["keep_score"] = None
        r["verdict"] = "⚪ No data"
        r["why"] = "not ingested yet — email the symbol once to build its report first"
    return scored + sorted(unscored, key=lambda r: r["symbol"])


# ══════════════════════ Version B — your cost, tax and "raise ₹X" ══════════════════════
# Built on your tax lots (portfolio/timeline.py): bonus shares are their own ₹0-cost lots dated on allotment,
# buys up to 31-Jan-2018 are grandfathered, and the gains you've already booked this financial year count
# (they use up the yearly exemption and can set off). Indian demat sales are FIFO by law (the oldest shares of
# a stock go first), so the choice is *which stocks and how many shares* — never which lot. Tax is an estimate
# (portfolio/tax.py). Undated buys: profit / loss known, tax not estimated.


def _book(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, dict], list[str], list[dict]]:
    """symbol → {name, ltp, value, lots: FIFO [{shares, cost_ps, acquired, term, days_to_long, fmv_ps}]} for
    priced holdings; the watchlist holdings with no quantity yet; and this year's booked gains."""
    from equity_research import portfolio

    p = portfolio.portfolio(con)
    today = date.today()
    book = {}
    for s in p["stocks"]:
        if not s["priced"]:
            continue
        parts = [pt for lt in s["lots"] if not lt.get("sold_out") for pt in lt["parts"]]
        parts.sort(key=lambda pt: (pt["acquired"] or "9999", pt["kind"] == "bonus"))   # FIFO; undated last
        lots = []
        for pt in parts:
            n = int(pt["shares"] + 1e-6)
            if n > 0:
                acq = date.fromisoformat(pt["acquired"]) if pt["acquired"] else None
                lots.append({"shares": n, "cost_ps": pt["cost_ps"], "acquired": acq, "kind": pt["kind"],
                             "term": tax.term(acq, today), "days_to_long": tax.days_to_long(acq, today),
                             "fmv_ps": pt["fmv_ps"]})
        if lots:
            book[s["symbol"]] = {"name": s["name"], "ltp": s["ltp"], "lots": lots,
                                 "value": sum(x["shares"] for x in lots) * s["ltp"]}
    no_qty = [sym for sym, _ in watchlist.entries_by_type(con, "holding") if sym not in book]
    return book, no_qty, portfolio.realised_this_year(p, today)


def _gain_ps(lot: dict, ltp: float) -> float:
    """Taxable gain per share sold at ``ltp`` (grandfathered cost for shares held since 31-Jan-2018)."""
    if lot["term"] == "long":
        return ltp - tax.tax_cost_ps(lot["cost_ps"], ltp, lot["acquired"], lot["fmv_ps"])
    return ltp - lot["cost_ps"]


def _marginal_rate(lot: dict, ltp: float, exemption_left: float) -> float:
    """Tax per ₹ of sale proceeds for the next share of this lot. A loss counts as zero, not a bonus:
    booking one only helps when there's a taxable gain to set it against (the final estimate does that
    set-off), so among zero-tax choices the weaker holding goes first rather than your losers."""
    from equity_research import config

    g = max(0.0, _gain_ps(lot, ltp) / ltp)
    if lot["term"] == "long":
        return 0.0 if exemption_left > 0 else g * config.LTCG_RATE
    return g * config.STCG_RATE           # short-term, and undated (assumed short-term: the cautious case)


def _plan(book: dict, amount: float, order: str, keep: dict, booked: list[dict]) -> dict:
    """Shares to sell to raise ``amount`` at the last close. ``order`` = 'tax' (the stock whose next
    FIFO shares cost the least tax per rupee first; weaker keep score breaks ties) or 'merit' (weakest
    keep score first). ``booked`` = gains already realised this financial year."""
    from equity_research import config

    ptr = {sym: [dict(lt) for lt in b["lots"]] for sym, b in book.items()}
    sold: dict[str, list[dict]] = {}
    need = amount
    ex_left = max(0.0, config.LTCG_EXEMPTION - tax_estimate(booked)["lt_gain"])
    merit_order = sorted(book, key=lambda s: (keep.get(s) is None, keep.get(s) or 0, s))
    while need > 0.5:
        live = [s for s in merit_order if ptr[s]]
        if not live:
            break
        if order == "merit":
            sym = live[0]
        else:
            sym = min(live, key=lambda s: (round(_marginal_rate(ptr[s][0], book[s]["ltp"], ex_left), 4),
                                          merit_order.index(s)))
        lot, ltp = ptr[sym][0], book[sym]["ltp"]
        n = min(lot["shares"], math.ceil(need / ltp))
        g = _gain_ps(lot, ltp)
        if order == "tax" and lot["term"] == "long" and g > 0 and ex_left > 0:
            n = min(n, max(1, int(ex_left / g)))                         # stay inside the exemption first
        gain = n * g
        sold.setdefault(sym, []).append({"shares": n, "gain": gain, "term": lot["term"], "kind": lot["kind"],
                                         "acquired": lot["acquired"], "days_to_long": lot["days_to_long"],
                                         "proceeds": n * ltp})
        if lot["term"] == "long" and gain > 0:
            ex_left -= gain
        need -= n * ltp
        lot["shares"] -= n
        if lot["shares"] <= 0:
            ptr[sym].pop(0)
    rows, tips = [], []
    for sym, parts in sold.items():
        b = book[sym]
        terms = {p["term"] for p in parts}
        rows.append({"symbol": sym, "name": b["name"], "ltp": b["ltp"], "keep": keep.get(sym),
                     "shares": sum(p["shares"] for p in parts), "held": sum(lt["shares"] for lt in b["lots"]),
                     "proceeds": sum(p["proceeds"] for p in parts), "gain": sum(p["gain"] for p in parts),
                     "term": next(iter(terms)) if len(terms) == 1 else "mixed", "parts": parts})
        for p in parts:
            if (p["term"] == "short" and p["gain"] > 0 and p["days_to_long"] is not None
                    and p["days_to_long"] <= config.LT_WAIT_TIP_DAYS):
                tips.append({"symbol": sym, "days": p["days_to_long"], "gain": p["gain"],
                             "save": p["gain"] * (config.STCG_RATE - config.LTCG_RATE) * (1 + config.TAX_CESS)})
    sales = [p for r in rows for p in r["parts"]]
    before, after = tax_estimate(booked), tax_estimate(booked + sales)
    t = {**tax_estimate(sales), "tax": after["tax"] - before["tax"],
         "exemption_used": after["exemption_used"] - before["exemption_used"]}
    proceeds = sum(r["proceeds"] for r in rows)
    return {"order": order, "rows": rows, "proceeds": proceeds, "short_by": max(0.0, amount - proceeds),
            "tax": t, "net": proceeds - t["tax"], "tips": tips,
            "has_unknown": any(p["term"] == "unknown" for p in sales)}


def raise_plan(con: duckdb.DuckDBPyConnection, amount: float, ranking: list[dict] | None = None) -> dict:
    """Two ways to raise ``amount`` from your holdings: 🧾 lowest tax and 💪 weakest first (the merit
    keep score from ``sell_ranking``). Returns {amount, book_value, plans: {tax, merit}, no_qty, same,
    booked}."""
    book, no_qty, booked = _book(con)
    keep = {r["symbol"]: r.get("keep_score") for r in (ranking or [])}
    plans = {o: _plan(book, amount, o, keep, booked) for o in ("tax", "merit")} if book else {}
    same = bool(plans) and ({(r["symbol"], r["shares"]) for r in plans["tax"]["rows"]}
                            == {(r["symbol"], r["shares"]) for r in plans["merit"]["rows"]})
    return {"amount": amount, "book_value": sum(b["value"] for b in book.values()), "plans": plans,
            "no_qty": no_qty, "same": same, "booked": tax_estimate(booked) if booked else None}


def book_summary(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """symbol → {value, pnl, pnl_pct, terms} for the plain `sell` table (holdings with quantities)."""
    from equity_research import portfolio

    out = {}
    for s in portfolio.portfolio(con)["stocks"]:
        if s["priced"]:
            terms = {pt["term"] if pt["term"] != "unknown" else "undated"
                     for lt in s["lots"] if not lt.get("sold_out") for pt in lt["parts"]}
            out[s["symbol"]] = {"value": s["value"], "pnl": s["pnl"], "pnl_pct": s["pnl_pct"], "terms": terms}
    return out
