"""Your whole portfolio: every stock's timeline replayed (``timeline.py``), valued on the latest close, with
realised gains by financial year, dividends and XIRR.

Companies that merged away are replayed up to their merger and carried into the survivor at the swap ratio
(``analysis/former_companies.py``); until the swap terms are read from the filings they're listed, unpriced, with
a note. The result also lists background lookups the page should start: demerger cost notices, merger terms,
dividend histories and the 31-Jan-2018 prices.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date

import duckdb

from equity_research.portfolio import income, instruments, store, tax
from equity_research.portfolio.timeline import Part, Result, walk

BENCHMARK = "Nifty 500"


def _bench_since(con, since: date) -> float | None:
    """The Nifty 500's move since ``since`` — None when the index history doesn't reach back that far (the
    first close must be within 10 days of the date, or the comparison would start years later)."""
    r = con.execute("""SELECT
            (SELECT close FROM index_close WHERE index_name = ? AND trade_date BETWEEN ? AND ? + INTERVAL 10 DAY
             ORDER BY trade_date LIMIT 1),
            (SELECT close FROM index_close WHERE index_name = ? ORDER BY trade_date DESC LIMIT 1)""",
                    [BENCHMARK, since, since, BENCHMARK]).fetchone()
    return (r[1] / r[0] - 1) if r and r[0] and r[1] else None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _part_view(p: Part, today: date) -> dict:
    return {"shares": p.shares, "cost_ps": p.cost / p.shares if p.shares else 0.0, "acquired": _iso(p.acquired),
            "kind": p.kind, "fmv_ps": p.fmv_ps(), "term": tax.term(p.acquired, today)}


def _lot_view(con, lot: dict, parts: list[Part], res: Result, ltp, today: date, *, via: dict | None = None,
              old_logs=None) -> dict:
    log = res.logs.get(lot["id"])
    mine = [p for p in parts if p.lot_id == lot["id"]]
    shares = sum(p.shares for p in mine)
    cost = sum(p.cost for p in mine)
    d = lot.get("buy_date")
    v = {**lot, "buy_date": _iso(d), "received": _iso(lot.get("received")), "adj_qty": shares,
         "adj_price": cost / shares if shares else None, "cost": cost, "sold_out": shares <= 1e-9,
         "adjusted": (old_logs.actions if old_logs else []) + (log.actions if log else []),
         "bonus": (old_logs.bonus if old_logs else []) + (log.bonus if log else []),
         "rights": log.rights if log else [], "demergers": log.demergers if log else [],
         "events": (old_logs.notes if old_logs else []) + (log.notes if log else []),
         "dividends": (old_logs.dividends if old_logs else 0) + (log.dividends if log else 0),
         "sold": (old_logs.sold if old_logs else 0) + (log.sold if log else 0),
         "parts": [_part_view(p, today) for p in mine]}
    if via:
        v["via"] = via
    if lot.get("from"):
        v["from"] = lot["from"]
    if ltp:
        v.update(ltp=ltp[0], ltp_date=ltp[1].isoformat(), value=shares * ltp[0])
        v.update(pnl=v["value"] - cost, pnl_pct=100 * (v["value"] / cost - 1) if cost else None)
    if d:
        days = (today - d).days
        v.update(days_held=days, term=tax.term(d, today), days_to_long=tax.days_to_long(d, today))
        if ltp and days >= tax.LONG_TERM_DAYS and cost > 0 and shares:
            v["yearly_pct"] = 100 * ((v["value"] / cost) ** (365 / days) - 1)
        b = _bench_since(con, d)
        if b is not None:
            v["bench_pct"] = 100 * b
    warn = (old_logs.warn if old_logs else None) or (log.warn if log else None)
    if warn:
        v["warn"] = warn
    if any(p["fmv_ps"] for p in v["parts"]):
        v["grandfathered"] = True
    return v


def _undated_view(lot: dict, ltp) -> tuple[dict, Part]:
    part = Part(lot["id"], lot["qty"], lot["qty"] * lot["price"], None)
    v = {**lot, "buy_date": None, "received": _iso(lot.get("received")), "adj_qty": lot["qty"],
         "adj_price": lot["price"], "cost": lot["qty"] * lot["price"], "adjusted": [], "bonus": [], "rights": [],
         "demergers": [], "dividends": 0.0, "sold": 0.0, "sold_out": False,
         "parts": [{"shares": lot["qty"], "cost_ps": lot["price"], "acquired": None, "kind": "bought",
                    "fmv_ps": None, "term": "unknown"}]}
    if ltp:
        v.update(ltp=ltp[0], ltp_date=ltp[1].isoformat(), value=lot["qty"] * ltp[0])
        v.update(pnl=v["value"] - v["cost"], pnl_pct=100 * (v["value"] / v["cost"] - 1))
    return v, part


def portfolio(con: duckdb.DuckDBPyConnection, *, today: date | None = None) -> dict:
    """Every stock: lots (with their history), value and P&L on the latest close, dividends, realised gains and
    XIRR; totals; realised gains by financial year; and the background lookups to start."""
    from equity_research.analysis import bse_actions, former_companies

    today = today or date.today()
    by_sym: dict[str, list[dict]] = defaultdict(list)
    sells_by: dict[str, list[dict]] = defaultdict(list)
    names: dict[str, str] = {}
    for lt in store.lots(con):
        by_sym[lt["symbol"]].append(lt)
        names.setdefault(lt["symbol"], lt["name"])
    for s in store.sells(con):
        sells_by[s["symbol"]].append(s)
        names.setdefault(s["symbol"], s["name"])

    injected: dict[str, list] = defaultdict(list)
    carried: dict[str, list] = defaultdict(list)        # survivor → [(old symbol, terms, old result, old lots)]
    stuck: dict[str, tuple] = {}                        # old symbol → (terms, note) — not convertible (yet)
    merger_lookups: list[str] = []
    for sym in sorted(set(by_sym) | set(sells_by)):             # still listed under another symbol → that one
        fc = former_companies.info(con, sym)
        live = former_companies.live_listing(con, fc) if fc else None
        if live:
            for lt in by_sym.pop(sym, []):
                by_sym[live[0]].append({**lt, "symbol": live[0], "name": live[1]})
            for s in sells_by.pop(sym, []):
                sells_by[live[0]].append({**s, "symbol": live[0], "name": live[1]})
            names.setdefault(live[0], live[1])
    for sym in sorted(set(by_sym) | set(sells_by)):
        fc = former_companies.info(con, sym)
        if not fc:
            continue
        md = fc.get("merger_date")
        if md and fc.get("into_symbol") and fc.get("ratio_new") and fc.get("ratio_old"):
            dated = [lt for lt in by_sym[sym] if lt.get("received") or lt.get("buy_date")]
            res = walk(con, sym, fc["name"], dated, sells_by[sym], today=today, until=md)
            ratio = fc["ratio_new"] / fc["ratio_old"]
            parts = res.parts + [Part(lt["id"], lt["qty"], lt["qty"] * lt["price"], None)
                                 for lt in by_sym[sym] if lt not in dated]
            for p in parts:
                p.shares *= ratio
                if md > tax.GRANDFATHER_DATE:
                    p.fmv_div *= ratio
            injected[fc["into_symbol"]].append((md, parts))
            carried[fc["into_symbol"]].append((sym, fc, res, by_sym[sym]))
            names.setdefault(fc["into_symbol"], fc["into_name"])
        else:
            if md and former_companies.needs_lookup(fc):
                merger_lookups.append(sym)
                note = f"{fc['name']} merged on {md:%d-%b-%Y} — reading its filings for what you got in exchange…"
            elif md:
                note = (f"{fc['name']} merged on {md:%d-%b-%Y}, but its filings don't say the swap ratio — enter the "
                        "shares you received under the new company instead.")
            else:
                note = f"{fc['name']} is no longer traded (last traded {fc.get('last_traded')})."
            stuck[sym] = (fc, note)

    # Demerged shares flow from the parent's timeline into the new company's: settle in passes (a parent's
    # buys / sells before the ex-date decide the new company's shares; a demerged company can demerge again).
    base = (set(by_sym) | set(sells_by) | set(injected)) - set(carried_old(carried)) - set(stuck)
    spawns: dict[str, list[dict]] = {}
    for _ in range(4):
        results = {}
        for sym in sorted(base | set(spawns)):
            lots = by_sym.get(sym, [])
            dated = [lt for lt in lots if lt.get("received") or lt.get("buy_date")]
            undated = [lt for lt in lots if lt not in dated]
            dupes = [lt for lt in dated if _replaced_by_spawn(lt, spawns.get(sym, []))]
            dated = [lt for lt in dated if lt not in dupes]
            inj = list(injected.get(sym, [])) + [(sp["ex"], [replace(pt) for pt in sp["parts"]])
                                                  for sp in spawns.get(sym, [])]
            for sp in spawns.get(sym, []):
                names.setdefault(sym, sp["name"])
            results[sym] = (walk(con, sym, names[sym], dated, sells_by.get(sym, []), today=today, injected=inj),
                            dated, undated, dupes)
        found: dict[str, list[dict]] = defaultdict(list)
        for res, *_ in results.values():
            for sp in res.spawned:
                found[sp["symbol"]].append(sp)
        if _sig(found) == _sig(spawns):
            break
        spawns = dict(found)

    stocks, realised, dividends_all, lookups, warns, all_flows = [], [], [], [], [], []
    for sym, (res, dated, undated, dupes) in sorted(results.items()):
        ltp = instruments.last_close(con, sym)
        views = [_lot_view(con, lt, res.parts, res, ltp, today) for lt in dated]
        for lt in _spawn_lots(sym, names[sym], spawns.get(sym, [])):
            v = _lot_view(con, lt, res.parts, res, ltp, today)
            if not v.get("sold_out") or v["sold"]:
                views.append(v)
        for lt in dupes:
            views.append({**lt, "buy_date": _iso(lt.get("buy_date")), "received": _iso(lt.get("received")),
                          "adj_qty": 0.0, "adj_price": None, "cost": 0.0, "value": 0.0, "adjusted": [], "bonus": [],
                          "rights": [], "demergers": [], "dividends": 0.0, "sold": 0.0, "sold_out": False,
                          "parts": [], "duplicate": True,
                          "note": "These shares are now worked out automatically from your buys of the parent company "
                                  "(see the 'from demerger' line) — this entry isn't counted; you can delete it."})
        for old_sym, fc, old_res, old_lots in carried.get(sym, []):
            via_base = {"symbol": old_sym, "name": fc["name"], "date": fc["merger_date"].isoformat(),
                        "url": fc.get("url"), "ratio": f"{fc['ratio_new']:g} for every {fc['ratio_old']:g}"}
            for lt in old_lots:
                v = _lot_view(con, lt, res.parts, res, ltp, today, old_logs=old_res.logs.get(lt["id"]),
                              via={**via_base, "shares": sum(p.shares for p in res.parts if p.lot_id == lt["id"])})
                v["symbol"], v["name"] = sym, names[sym]
                views.append(v)
            realised += old_res.realised
            dividends_all += [{**d, "symbol": sym} for d in old_res.dividends]
            res.flows += old_res.flows
            warns += old_res.warns
        old_divs = sum(d["amount"] for _, _, o, _ in carried.get(sym, []) for d in o.dividends)
        undated_parts = []
        for lt in undated:
            v, part = _undated_view(lt, ltp)
            views.append(v)
            undated_parts.append(part)
        realised += res.realised
        dividends_all += [{**d, "symbol": sym} for d in res.dividends]
        lookups += res.lookups
        warns += res.warns
        live = [v for v in views if not v.get("sold_out")]
        qty = sum(v["adj_qty"] for v in live)
        cost = sum(v["cost"] for v in live)
        s = {"symbol": sym, "name": names[sym], "lots": views, "qty": qty, "cost": cost, "priced": bool(ltp),
             "ltp": ltp[0] if ltp else None, "kind": _kind(con, sym),
             "dividends": sum(d["amount"] for d in res.dividends) + old_divs,
             "realised_gain": sum(r["gain"] or 0 for r in res.realised)
             + sum(r["gain"] or 0 for _, _, o, _ in carried.get(sym, []) for r in o.realised)}
        if ltp:
            s["value"] = qty * ltp[0]
            s["pnl"] = s["value"] - cost
            s["pnl_pct"] = 100 * (s["value"] / cost - 1) if cost else None
            dated_value = sum(p.shares for p in res.parts) * ltp[0]
            s["xirr_pct"] = _pct(income.xirr(res.flows + [(today, dated_value)]))
        s["avg_price"] = cost / qty if qty else None
        all_flows += res.flows + ([(today, sum(p.shares for p in res.parts) * ltp[0])] if ltp else [])
        if qty > 1e-9:                      # fully sold stocks live on in the realised table
            stocks.append(s)

    for sym, (fc, note) in stuck.items():
        views = []
        for lt in by_sym.get(sym, []):
            views.append({**lt, "buy_date": _iso(lt.get("buy_date")), "received": _iso(lt.get("received")),
                          "adj_qty": lt["qty"], "adj_price": lt["price"], "cost": lt["qty"] * lt["price"],
                          "adjusted": [], "bonus": [], "rights": [], "demergers": [], "dividends": 0.0, "sold": 0.0,
                          "sold_out": False, "note": note, "parts": []})
        if views:
            stocks.append({"symbol": sym, "name": fc["name"], "lots": views, "qty": sum(v["adj_qty"] for v in views),
                           "cost": sum(v["cost"] for v in views), "priced": False, "ltp": None, "kind": "former",
                           "dividends": 0.0, "realised_gain": 0.0, "avg_price": None})

    priced = [s for s in stocks if s["priced"]]
    cost = sum(s["cost"] for s in priced)
    value = sum(s["value"] for s in priced)
    for s in priced:
        s["weight_pct"] = 100 * s["value"] / value if value else None
    syms = sorted({s["symbol"] for s in stocks if s["kind"] != "former"})
    fmv_needed = any(p["acquired"] and p["acquired"] <= tax.GRANDFATHER_DATE.isoformat()
                     for s in stocks for v in s["lots"] for p in v["parts"])
    return {
        "stocks": sorted(stocks, key=lambda s: -(s.get("value") or 0)),
        "missing": [m for m in store.missing(con)                  # held already (e.g. through a demerger) → not missing
                    if m["symbol"] not in {s["symbol"] for s in stocks}],
        "lookups": sorted(set(lookups)),
        "merger_lookups": sorted(set(merger_lookups)),
        "dividend_refresh": income.stale(con, syms),
        "bse_action_refresh": bse_actions.stale(con, syms),
        "fmv_pending": fmv_needed and not tax.fmv_loaded(con),
        "realised": by_year(realised, dividends_all),
        "sells": [{**s, "sell_date": s["sell_date"].isoformat()} for ss in sells_by.values() for s in ss],
        "warns": warns,
        "total": {"cost": cost, "value": value, "pnl": value - cost,
                  "pnl_pct": 100 * (value / cost - 1) if cost else None, "n_stocks": len(stocks),
                  "n_lots": sum(1 for s in stocks for v in s["lots"]
                                if not v.get("sold_out") and not v.get("duplicate")),
                  "unpriced": [s["symbol"] for s in stocks if not s["priced"]],
                  "dividends": sum(d["amount"] for d in dividends_all),
                  "realised_gain": sum(r["gain"] or 0 for r in realised),
                  "xirr_pct": _pct(income.xirr(all_flows))},
    }


def _sig(spawns: dict[str, list[dict]]) -> tuple:
    """A comparable fingerprint of the demerged shares found (to know when the passes have settled)."""
    return tuple(sorted((sym, sp["ex"], pt.lot_id, round(pt.shares, 6), round(pt.cost, 4))
                        for sym, sps in spawns.items() for sp in sps for pt in sp["parts"]))


def _spawn_lots(sym: str, name: str, spawns: list[dict]) -> list[dict]:
    """One entry per parent buy for the shares a demerger gave you — shown, not edited (source 'auto')."""
    out = {}
    for sp in spawns:
        for pt in sp["parts"]:
            lot = out.setdefault(pt.lot_id, {"id": pt.lot_id, "symbol": sym, "name": name, "qty": 0.0, "cost": 0.0,
                                             "buy_date": pt.acquired, "source": "auto", "received": sp["ex"],
                                             "from": {"symbol": sp["parent"], "name": sp["parent_name"],
                                                      "date": sp["ex"].isoformat(), "pct": sp["pct"],
                                                      "url": sp["url"]}})
            lot["qty"] += pt.shares
            lot["cost"] += pt.cost
            if pt.acquired and (not lot["buy_date"] or pt.acquired < lot["buy_date"]):
                lot["buy_date"] = pt.acquired
    for lot in out.values():
        lot["price"] = lot.pop("cost") / lot["qty"] if lot["qty"] else 0.0
    return list(out.values())


def _replaced_by_spawn(lot: dict, spawns: list[dict]) -> bool:
    """An entry typed (or added with the old one-off button) for shares a demerger gave — the automatic carry
    replaces it: same company, received on that demerger's ex-date."""
    return bool(lot.get("received")) and lot.get("source") == "ui" and any(sp["ex"] == lot["received"]
                                                                            for sp in spawns)


def carried_old(carried: dict) -> list[str]:
    return [old for lst in carried.values() for old, _, _, _ in lst]


def _kind(con, sym: str) -> str:
    r = con.execute("SELECT kind FROM instruments WHERE symbol = ?", [sym]).fetchone()   # bse_suspended included
    return r[0] if r else "share"


def _pct(x: float | None) -> float | None:
    return None if x is None else 100 * x


def by_year(realised: list[dict], dividends: list[dict]) -> list[dict]:
    """Realised gains per financial year: each sale (slice), totals, the estimated tax on them, dividends."""
    years: dict[str, dict] = {}
    for r in realised:
        y = years.setdefault(r["fy"], {"fy": r["fy"], "rows": [], "dividends": 0.0, "deemed_dividend": 0.0})
        y["rows"].append({**r, "date": r["date"].isoformat(), "acquired": _iso(r.get("acquired"))})
        y["deemed_dividend"] += r.get("deemed_dividend") or 0
    for d in dividends:
        y = years.setdefault(tax.fy_label(d["date"]), {"fy": tax.fy_label(d["date"]), "rows": [], "dividends": 0.0,
                                                       "deemed_dividend": 0.0})
        y["dividends"] += d["amount"]
    out = []
    for y in years.values():
        taxable = [{"gain": r["gain"], "term": r["term"]} for r in y["rows"]
                   if r["gain"] is not None and r["treatment"] in ("capital_gains", "dividend")]
        y["tax"] = tax.tax_estimate(taxable)
        y["exempt_buyback"] = sum(r["gain"] or 0 for r in y["rows"] if r["treatment"] == "exempt")
        y["unmatched"] = sum(1 for r in y["rows"] if r["treatment"] == "unmatched")
        y["rows"].sort(key=lambda r: r["date"])
        out.append(y)
    return sorted(out, key=lambda y: y["fy"][-7:], reverse=True)


def realised_this_year(p: dict, today: date | None = None) -> list[dict]:
    """[{gain, term}] already booked in the current financial year — so a new plan's tax counts the year's
    exemption and set-off correctly."""
    fy = tax.fy_label(today or date.today())
    year = next((y for y in p["realised"] if y["fy"] == fy), None)
    return [{"gain": r["gain"], "term": r["term"]} for r in (year or {}).get("rows", [])
            if r["gain"] is not None and r["treatment"] in ("capital_gains", "dividend")]


def value_lot(con: duckdb.DuckDBPyConnection, lot: dict, *, today: date | None = None) -> dict:
    """One buy on its own (no sells, no other buys) — handy for checks and tests; the page uses ``portfolio``."""
    today = today or date.today()
    ltp = instruments.last_close(con, lot["symbol"])
    if not (lot.get("received") or lot.get("buy_date")):
        return _undated_view(lot, ltp)[0]
    res = walk(con, lot["symbol"], lot["name"], [lot], [], today=today)
    return _lot_view(con, lot, res.parts, res, ltp, today)
