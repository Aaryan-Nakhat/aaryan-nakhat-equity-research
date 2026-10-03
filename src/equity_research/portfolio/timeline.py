"""One stock's history, replayed in date order → your tax lots today, what you sold, and what you received.

Every dated buy becomes a **part** (shares, cost, the date that decides its tax term). Then, in date order:

* **split / consolidation** — every part's shares multiply; cost and date unchanged.
* **bonus** — each part spawns a new part of ``shares × (m − 1)`` at **₹0 cost, dated on the ex-date** (the tax
  rule; brokers average it in, which is why their per-share average drops — the totals here match theirs).
* **rights issue** — nothing changes on its own: whether you subscribed is yours to say. Each lot gets an
  offer (entitled shares, the issue price when known) to add as a new buy.
* **demerger** — every part keeps the parent's share of cost from the company's filed notice (else the market
  estimate) and, when the notice names a listed new company and the ratio, **spawns** that company's shares:
  same acquisition date, the moved share of cost, no cash paid. They're carried into the new company's own
  timeline (``valuation.py``), so later sells / buys of the parent before the ex-date are reflected automatically
  (``analysis/demerger_costs.py``).
* **dividend** — the shares held just before the ex-date × the amount, credited per lot (``income.py``).
* **sell** — FIFO across parts by acquisition date (Indian demat rule), each slice taxed by its own term, with
  31-Jan-2018 grandfathering; a buyback by the rules of its date (``tax.py``).

Same-day order: dividends (entitlement is fixed the day before), then the ex-date action, then buys, then sells.
Undated buys aren't replayed — they're today's numbers (the broker's qty and average) and sells don't touch them.
A company that merged away is replayed up to the merger, its parts converted at the swap ratio and carried
into the survivor's timeline (``valuation.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

from equity_research.portfolio import income, tax

_ORDER = {"split": 0, "consolidation": 0, "bonus": 1, "rights": 2, "demerger": 3}
SUSPECT_BELOW = 0.6      # a dated buy priced under 60% of the market price then, with a split since → flagged
_RIGHTS = re.compile(r"rights?\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)(?:.*?premium\s*r[se]\.?\s*(\d+(?:\.\d+)?))?",
                     re.I)


@dataclass
class Part:
    lot_id: str
    shares: float
    cost: float                      # ₹ for these shares
    acquired: date | None            # decides the tax term
    kind: str = "bought"             # bought | bonus
    fmv_high: float | None = None    # 31-Jan-2018 high of the share, for parts acquired by then
    fmv_div: float = 1.0             # share multiplications since 31-Jan-2018: FMV per share now = high / div

    def fmv_ps(self) -> float | None:
        return self.fmv_high / self.fmv_div if self.fmv_high else None


@dataclass
class LotLog:
    """What happened to one buy along the way (for the page)."""
    actions: list[str] = field(default_factory=list)
    bonus: list[dict] = field(default_factory=list)
    rights: list[dict] = field(default_factory=list)
    demergers: list[dict] = field(default_factory=list)
    dividends: float = 0.0
    sold: float = 0.0
    warn: str | None = None
    notes: list[str] = field(default_factory=list)   # events that change shares but couldn't be sized
    first_action: date | None = None     # the first split / bonus since the buy (for the "today's numbers" check)


@dataclass
class Result:
    parts: list[Part] = field(default_factory=list)            # what's left, dated
    logs: dict[str, LotLog] = field(default_factory=dict)
    realised: list[dict] = field(default_factory=list)
    dividends: list[dict] = field(default_factory=list)          # [{date, amount}]
    flows: list[tuple[date, float]] = field(default_factory=list)
    lookups: list[tuple] = field(default_factory=list)           # demerger notices to read
    warns: list[str] = field(default_factory=list)
    spawned: list[dict] = field(default_factory=list)            # demerged shares → the new company's timeline


def _actions(con, symbol: str, after: date, until: date) -> list[tuple]:
    return con.execute("""SELECT ex_date, kind, share_mult, factor, detail FROM price_adjustments
                          WHERE symbol = ? AND ex_date > ? AND ex_date <= ? ORDER BY ex_date""",
                       [symbol, after, until]).fetchall()


def _face_value(con, symbol: str) -> float | None:
    r = con.execute("""SELECT arg_max(value, period_end) FROM financials WHERE symbol = ?
                       AND element = 'FaceValueOfEquityShareCapital' AND value > 0""", [symbol]).fetchone()
    return float(r[0]) if r and r[0] else None


def _raw_close_near(con, symbol: str, d: date, before: date) -> float | None:
    """The unadjusted close nearest ``d`` (within ~2 months — history can have gaps), before ``before``."""
    r = con.execute("""SELECT close FROM equity_eod WHERE symbol = ? AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
                       AND trade_date BETWEEN ? - INTERVAL 60 DAY AND ? + INTERVAL 60 DAY AND trade_date < ?
                       ORDER BY abs(trade_date - ?), CASE series WHEN 'EQ' THEN 0 ELSE 1 END LIMIT 1""",
                    [symbol, d, d, before, d]).fetchone()
    return float(r[0]) if r and r[0] else None


def _lot_shares(parts: list[Part], lot_id: str) -> float:
    return sum(p.shares for p in parts if p.lot_id == lot_id)


def walk(con: duckdb.DuckDBPyConnection, symbol: str, name: str, lots: list[dict], sells: list[dict], *,
         today: date, until: date | None = None, injected: list[tuple[date, list[Part]]] = ()) -> Result:
    """Replay ``symbol``'s dated buys, corporate actions, dividends and sells up to ``until`` (default today)."""
    from equity_research.analysis import demerger_costs as dc

    until = until or today
    res = Result()
    fmv_high = tax.fmv_for(con, symbol)
    events: list[tuple] = []
    starts = []
    for lot in lots:
        start = lot.get("received") or lot.get("buy_date")
        if start and start <= until:
            starts.append(start)
            events.append((start, 2, "buy", lot))
            res.logs[lot["id"]] = LotLog()
    for d, parts in injected:
        starts.append(d)
        events.append((d, 2, "inject", parts))
        for p in parts:
            res.logs.setdefault(p.lot_id, LotLog())
    for s in sells:
        if s["sell_date"] <= until:
            events.append((s["sell_date"], 3, "sell", s))
    if not starts:
        res.realised += [_unmatched(s, symbol, name, s["qty"]) for s in sells if s["sell_date"] <= until]
        return res
    first = min(starts) - timedelta(days=1)
    for ex, kind, mult, factor, detail in _actions(con, symbol, first, until):
        events.append((ex, 1, "action", (kind, mult, factor, detail)))
    for ex, amt in income.for_symbol(con, symbol, first, until):
        events.append((ex, 0, "div", amt))
    events.sort(key=lambda e: (e[0], e[1]))

    parts = res.parts
    for when, _, what, x in events:
        if what == "buy":
            acquired = x.get("buy_date")
            parts.append(Part(x["id"], x["qty"], x["qty"] * x["price"], acquired,
                              fmv_high=fmv_high if acquired and acquired <= tax.GRANDFATHER_DATE else None))
            if not x.get("received"):
                res.flows.append((acquired or when, -x["qty"] * x["price"]))
        elif what == "inject":
            parts.extend(x)
        elif what == "div":
            got = 0.0
            for lot_id in {p.lot_id for p in parts}:
                amt = _lot_shares(parts, lot_id) * x
                res.logs[lot_id].dividends += amt
                got += amt
            if got:
                res.dividends.append({"date": when, "amount": got, "per_share": x})
                res.flows.append((when, got))
        elif what == "action":
            _apply(con, symbol, name, when, x, parts, res, fmv_high, dc)
        elif what == "sell":
            _sell(x, parts, res, symbol, name)

    # flag today's numbers typed with an old date (a split / bonus since, and a price far below the market then)
    for lot in lots:
        log = res.logs.get(lot["id"])
        if log and log.actions and not lot.get("received") and lot.get("buy_date"):
            then = _raw_close_near(con, symbol, lot["buy_date"], log.first_action)
            if then and lot["price"] < SUSPECT_BELOW * then:
                log.warn = (f"₹{lot['price']:,.2f} is far below the price around {lot['buy_date']:%d-%b-%Y} "
                            f"(≈₹{then:,.2f}), and there's been a split / bonus since — this looks like today's "
                            f"numbers. Enter the quantity and price as you bought them, or remove the date.")
    return res


def decompose(kind: str, mult: float | None, factor: float | None, detail: str | None
              ) -> list[tuple[str, float | None, float | None, str]]:
    """One ex-date's record → the actions to apply, in order (splits, then bonuses, rights, demergers).

    A combined record ('split+bonus', 'bonus+rights', 'demerger+bonus' …) keeps each part's NSE wording in
    ``detail`` (joined by ' — '), so each part is re-read and applied on its own. Anything that can't be split
    into known parts — or whose parts don't add up to the recorded share multiplier — becomes 'unsized' (a
    visible note), never a silent skip. 'split/bonus' (a price gap the exchange never explained) is applied as
    a split, with a note that it may have been a bonus."""
    from equity_research.analysis import corporate_actions as ca

    if kind in ("split", "consolidation", "bonus", "rights", "demerger", "unsized"):
        return [(kind, mult, factor, detail or "")]
    if kind == "split/bonus":
        return [("split", mult, factor, detail or ""),
                ("unsized", None, None, "A price gap the exchange never explained, applied as a split — if it was a "
                                        "bonus, the extra shares cost nothing and are dated on that day")]
    if "+" not in kind:
        return [("unsized", None, None, f"{kind} ({detail or 'corporate action'})")]
    pieces = []
    for seg in str(detail or "").split(" — "):
        got = ca.parse_subject(seg)
        if got:
            k, f = got
            m = (1 / f) if f else None
            if any(k == k2 and m is not None and m == m2 for k2, m2, _ in pieces):
                continue                         # the same action filed twice under different wording
            pieces.append((k, m, seg))
    kinds = sorted({k for k, _, _ in pieces})
    if sorted(set(kind.split("+"))) != kinds:
        return [("unsized", None, None, f"{kind} ({detail or 'corporate action'})")]
    fixed = [m for k, m, _ in pieces if k in ("split", "consolidation", "bonus") and m]
    expect = 1.0
    for m in fixed:
        expect *= m
    has_rights = any(k == "rights" for k, _, _ in pieces)
    if mult and not has_rights and abs(expect - mult) > 1e-6 * max(1.0, mult) and len(set(kind.split("+"))) == len(
            kind.split("+")):
        return [("unsized", None, None, f"{kind} ({detail}) — its parts don't match the recorded share change")]
    out = []
    for k, m, seg in sorted(pieces, key=lambda x: _ORDER.get(x[0], 9)):
        if k == "demerger":                     # the day's price drop also carries the split / bonus
            out.append((k, 1.0, (factor * expect) if factor else None, seg))
        else:
            out.append((k, m, None, seg))
    return out


def _apply(con, symbol, name, ex: date, action: tuple, parts: list[Part], res: Result, fmv_high, dc) -> None:
    for sub in decompose(*action):
        _apply_one(con, symbol, name, ex, sub, parts, res, fmv_high, dc)


def _apply_one(con, symbol, name, ex: date, action: tuple, parts: list[Part], res: Result, fmv_high, dc) -> None:
    kind, mult, factor, detail = action
    lot_ids = sorted({p.lot_id for p in parts})
    if kind == "bonus" and mult and mult > 1:
        for p in list(parts):
            if p.shares <= 0:
                continue
            extra = p.shares * (mult - 1)
            parts.append(Part(p.lot_id, extra, 0.0, ex, "bonus",
                              fmv_high=fmv_high if ex <= tax.GRANDFATHER_DATE else None))
        for lot_id in lot_ids:
            got = sum(p.shares for p in parts if p.lot_id == lot_id and p.kind == "bonus" and p.acquired == ex)
            res.logs[lot_id].bonus.append({"date": ex.isoformat(), "shares": got})
            _mark(res.logs[lot_id], ex, f"bonus (ex {ex:%d-%b-%Y}, +{mult - 1:g} per share, ₹0 cost)")
    elif kind in ("split", "consolidation") and mult and abs(mult - 1) > 1e-9:
        for p in parts:
            p.shares *= mult
            if ex > tax.GRANDFATHER_DATE:
                p.fmv_div *= mult
        for lot_id in lot_ids:
            _mark(res.logs[lot_id], ex, f"{kind} (ex {ex:%d-%b-%Y}, ×{mult:.3g} shares)")
    elif kind == "rights":
        m = _RIGHTS.search(str(detail or ""))
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            fv = _face_value(con, symbol)
            issue = (fv + float(m.group(3))) if fv and m.group(3) else None
            per_share, ratio = a / b, f"{a:g} for every {b:g}"
        elif mult and mult > 1:                        # BSE: ratio known as a multiplier only
            per_share, ratio, issue = mult - 1, f"{mult - 1:g} per share held", None
        else:                                          # BSE often doesn't state it
            per_share, ratio, issue = None, "ratio not stated", None
        for lot_id in lot_ids:
            entitled = int(_lot_shares(parts, lot_id) * per_share + 1e-9) if per_share else None
            res.logs[lot_id].rights.append({"date": ex.isoformat(), "ratio": ratio, "entitled": entitled,
                                            "price": issue})
    elif kind == "unsized":
        src = "BSE record" if "(BSE)" in str(detail) else "exchange record"
        for lot_id in lot_ids:
            res.logs[lot_id].notes.append(
                f"{str(detail or 'Corporate action').replace(' (BSE)', '')} on {ex:%d-%b-%Y} ({src}) may have "
                "changed your share count or cost — it isn't fully applied here. Check this buy against your broker "
                "and, if it changed, enter the buy without a date as your broker shows it today.")
    elif kind == "demerger":
        split = dc.known(con, symbol, ex)
        if split is None:
            res.lookups.append((symbol, name, ex))
        if split:
            keep, basis = 1 - sum(c["cost_pct"] for c in split) / 100, "filing"
        elif factor:
            keep, basis = factor, ("looking" if split is None else "estimate")
        else:
            keep, basis = 1.0, ("looking" if split is None else "unknown")
        for lot_id in lot_ids:
            lot_cost = sum(p.cost for p in parts if p.lot_id == lot_id)
            held = _lot_shares(parts, lot_id)
            item = {"ex_date": ex.isoformat(), "basis": basis, "children": [],
                    "parent_pct": 100 * keep if basis != "unknown" and (split or factor) else None,
                    "url": split[0]["url"] if split else None}
            for c in split or []:
                n = held * c["ratio_new"] / c["ratio_old"] if c["ratio_new"] and c["ratio_old"] else None
                c_cost = lot_cost * c["cost_pct"] / 100
                item["children"].append({"symbol": c["new_symbol"], "name": c["new_name"], "pct": c["cost_pct"],
                                         "qty": n, "cost": c_cost, "price": c_cost / n if n else None,
                                         "carried": bool(c["new_symbol"] and n)})
            res.logs[lot_id].demergers.append(item)
        for c in split or []:                       # the new company's shares, part by part (same date, ₹ share)
            if not (c["new_symbol"] and c["ratio_new"] and c["ratio_old"]):
                continue
            ratio = c["ratio_new"] / c["ratio_old"]
            kids = [Part(f"{p.lot_id}>{c['new_symbol']}", p.shares * ratio, p.cost * c["cost_pct"] / 100, p.acquired,
                         p.kind) for p in parts if p.shares > 1e-9]
            res.spawned.append({"symbol": c["new_symbol"], "name": c["new_name"], "ex": ex, "parts": kids,
                                "parent": symbol, "parent_name": name, "pct": c["cost_pct"],
                                "url": split[0]["url"]})
        for p in parts:
            p.cost *= keep


def _mark(log: LotLog, ex: date, label: str) -> None:
    log.first_action = log.first_action or ex
    log.actions.append(label)


def _unmatched(s: dict, symbol: str, name: str, qty: float) -> dict:
    return {"sell_id": s["id"], "date": s["sell_date"], "symbol": symbol, "name": name, "lot_id": None,
            "shares": qty, "proceeds": qty * s["price"], "cost": None, "gain": None, "term": "unknown",
            "kind": s["kind"], "treatment": "unmatched", "fy": tax.fy_label(s["sell_date"]), "acquired": None}


def _sell(s: dict, parts: list[Part], res: Result, symbol: str, name: str) -> None:
    """FIFO: the oldest shares (by acquisition date) go first."""
    order = sorted((p for p in parts if p.shares > 1e-9),
                   key=lambda p: (p.acquired or date.max, p.kind == "bonus"))
    left = s["qty"]
    sold_on, price = s["sell_date"], s["price"]
    treatment = tax.buyback_treatment(sold_on) if s["kind"] == "buyback" else "capital_gains"
    for p in order:
        if left <= 1e-9:
            break
        n = min(p.shares, left)
        cost = p.cost * n / p.shares
        proceeds = n * price
        trm = tax.term(p.acquired, sold_on)
        tax_cost = n * tax.tax_cost_ps(cost / n, price, p.acquired, p.fmv_ps()) if trm == "long" else cost
        entry = {"sell_id": s["id"], "date": sold_on, "symbol": symbol, "name": name, "lot_id": p.lot_id,
                 "shares": n, "proceeds": proceeds, "actual_cost": cost, "cost": tax_cost, "term": trm,
                 "acquired": p.acquired, "kind": s["kind"], "treatment": treatment, "fy": tax.fy_label(sold_on),
                 "grandfathered": tax_cost > cost + 1e-6}
        if treatment == "dividend":            # proceeds were a deemed dividend; the cost a capital loss
            entry.update(gain=-cost, deemed_dividend=proceeds)
        else:
            entry["gain"] = proceeds - tax_cost
        res.realised.append(entry)
        res.logs[p.lot_id].sold += n
        p.cost -= cost
        p.shares -= n
        left -= n
    res.flows.append((sold_on, s["qty"] * price))
    if left > 1e-6:
        res.realised.append(_unmatched(s, symbol, name, left))
        res.warns.append(f"Sold {left:,.4g} more {name} shares on {sold_on:%d-%b-%Y} than your dated buys cover — "
                         "add the earlier buys (with dates) so the tax can be worked out.")
    parts[:] = [p for p in parts if p.shares > 1e-9]
