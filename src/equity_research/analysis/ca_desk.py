"""📋 Corporate-Action Desk — "something's happening to a stock I own: do I need to do anything?"

For each stock you hold (the watchlist's holdings + any stock with a thesis), it lists the corporate
actions from NSE's record that are coming up or just happened, and says plainly whether you need to act:

* **Buyback (tender)** — act: the offer price vs today's price, the tender window and the small-shareholder
  entitlement, read from the letter of offer / public announcement.
* **Rights issue** — act: what each right is worth (price − issue price), what ignoring it costs you (the
  dilution to the theoretical ex-rights price), and the application / renunciation dates.
* **Demerger** — usually nothing to do, but your cost gets split: the ratio, the new company, its listing
  date and the cost-of-acquisition split the company files.
* **Bonus / split** — nothing to do: your share count multiplies, the price and your cost per share divide.
* **Dividend** — nothing to do: the amount, ex-date and record date.

Dates, ratios and prices for buybacks, rights and demergers aren't in NSE's one-line record, so the
matching filing PDFs are read by the LLM (``synthesize.ca_details``), each figure cited to its filing.
It never says whether to tender or apply — it lays out the numbers. No tax treatment is asserted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import duckdb

from equity_research.analysis import corporate_actions as ca

log = logging.getLogger(__name__)
WINDOW_BACK, WINDOW_AHEAD = 45, 90          # days either side of today
_KEYWORDS = {"buyback": ("buyback", "buy back", "buy-back", "tender offer", "letter of offer", "public announcement"),
             "rights": ("rights issue", "rights entitlement", "letter of offer", "renunciation", "rights"),
             "demerger": ("demerger", "scheme of arrangement", "cost of acquisition", "record date", "listing")}
CLOSED_AFTER_DAYS = 30   # no closing date known: a buyback / rights this far past its record date is over
ACT = {"buyback": True, "rights": True, "demerger": False, "bonus": False, "split": False, "dividend": False}


@dataclass
class Action:
    symbol: str
    name: str
    kind: str                   # buyback · rights · demerger · bonus · split · dividend · other
    subject: str
    ex_date: date | None
    record_date: date | None
    details: dict = field(default_factory=dict)     # extracted / computed specifics
    evidence: dict = field(default_factory=dict)    # id → {text, url}
    price: float | None = None

    @property
    def closed_on(self) -> date | None:
        """When a buyback / rights window closed, if it's already shut (else None)."""
        if not ACT.get(self.kind):
            return None
        end = _d(self.details.get("close_date"))
        today = date.today()
        if end:
            return end if end < today else None
        # no closing date read: treat a record date more than a month back as over
        ref = self.record_date or self.ex_date
        return ref if ref and (today - ref).days > CLOSED_AFTER_DAYS else None

    @property
    def needs_action(self) -> bool:
        return ACT.get(self.kind, False) and self.closed_on is None

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.kind}|{self.ex_date}"


def _d(s) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _kind(subject: str) -> str:
    s = subject.lower()
    if any(w in s for w in (*ca._NOT_EQUITY, "ccps")):
        return "other"
    if "buy back" in s or "buyback" in s or "buy-back" in s:
        return "buyback"
    if "right" in s:
        return "rights"
    if "demerger" in s:
        return "demerger"
    if "bonus" in s:
        return "bonus"
    if "split" in s or "sub-division" in s or "consolidation" in s:
        return "split"
    if "dividend" in s:
        return "dividend"
    return "other"


def holdings(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """(symbol, name) of what you hold: watchlist holdings + stocks with an active thesis."""
    rows = con.execute(
        """SELECT w.symbol, coalesce(m.company_name, w.symbol) FROM watchlist w
           LEFT JOIN equity_master m ON m.symbol = w.symbol
           WHERE coalesce(w.list_type, 'holding') = 'holding'
           UNION SELECT symbol, name FROM theses WHERE active""").fetchall()
    return sorted({(s, n) for s, n in rows if s})


def _last_close(con, symbol: str) -> float | None:
    r = con.execute("SELECT close FROM equity_eod_adj WHERE symbol = ? AND series IN ('EQ','BE','BZ') "
                    "ORDER BY trade_date DESC LIMIT 1", [symbol]).fetchone()
    return float(r[0]) if r else None


def actions_for(con: duckdb.DuckDBPyConnection, symbol: str, name: str, *, today: date | None = None,
                rows: list | None = None) -> list[Action]:
    """This holding's corporate actions from NSE's record within the window (``rows`` injectable)."""
    from equity_research.scrapers import nse_api

    today = today or date.today()
    if rows is None:
        try:
            rows = nse_api.corporate_actions_symbol(symbol)
        except Exception:  # noqa: BLE001 — NSE access off or a fetch failure
            return []
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        subj = " ".join(str(r.get("subject") or "").split())
        ex = _d(r.get("exDate"))
        if not subj or not ex or not (today - timedelta(days=WINDOW_BACK) <= ex <= today + timedelta(days=WINDOW_AHEAD)):
            continue
        kind = _kind(subj)
        if kind == "other" or (kind, ex) in seen:
            continue
        seen.add((kind, ex))
        a = Action(symbol, name, kind, subj, ex, _d(r.get("recDate")), price=_last_close(con, symbol))
        _fixed_details(a)
        out.append(a)
    return sorted(out, key=lambda a: (not a.needs_action, a.ex_date or today))


def _fixed_details(a: Action) -> None:
    """What the one-line subject already says (bonus / split ratio, dividend amount, rights ratio)."""
    parsed = ca.parse_subject(a.subject)
    if a.kind in ("bonus", "split") and parsed and parsed[1]:
        mult = 1 / parsed[1]
        a.details.update(share_multiplier=round(mult, 4),
                         meaning=f"your share count ×{mult:g}; the price and your cost per share ÷{mult:g}")
    elif a.kind == "dividend":
        m = re.search(r"r[se]\.?\s*([\d.]+)", a.subject, re.I)
        if m:
            a.details["per_share"] = float(m.group(1))
            if a.price:
                a.details["yield_pct"] = round(100 * float(m.group(1)) / a.price, 2)
    elif a.kind == "rights":
        m = ca._RIGHTS.search(a.subject)
        if m:
            a.details.update(ratio=f"{m.group(1)} new for every {m.group(2)} held", _a=float(m.group(1)),
                             _b=float(m.group(2)), premium=float(m.group(3)))


def enrich(con: duckdb.DuckDBPyConnection, a: Action, face_values: dict[str, float] | None = None) -> None:
    """For buybacks, rights and demergers: read the matching filings for the specifics, then compute."""
    from equity_research.analysis import reality_check
    from equity_research.common.http import fetch_bytes
    from equity_research.reports import synthesize

    if a.kind not in ("buyback", "rights", "demerger"):
        return
    words = _KEYWORDS[a.kind]
    filings = [f for f in (reality_check._filings(a.symbol) or []) if any(w in f["text"].lower() for w in words)]
    ev, files = {}, []
    for i, f in enumerate(filings[:8], 1):
        ev[f"F{i}"] = {"text": (f"{f['date']:%d-%b-%Y} " if f["date"] else "") + f["text"], "url": f["url"]}
        if f["url"].lower().endswith(".pdf") and len(files) < 3:
            try:
                files.append((f"F{i}.pdf", fetch_bytes(f["url"])))
            except Exception:  # noqa: BLE001
                pass
    a.evidence = ev
    got = synthesize.ca_details(a.kind, a.name, a.subject, {k: v["text"] for k, v in ev.items()}, files=files) or {}
    a.details.update({k: v for k, v in got.items() if v not in (None, "", [])})
    if a.kind == "rights":
        fv = (face_values or {}).get(a.symbol) or got.get("face_value")
        issue = got.get("issue_price") or ((fv + a.details["premium"]) if fv and "premium" in a.details else None)
        if issue and a.price and "_a" in a.details:
            aa, bb = a.details["_a"], a.details["_b"]
            terp = (bb * a.price + aa * issue) / (aa + bb)
            a.details.update(issue_price=issue, value_per_right=round(a.price - issue, 2),
                             terp=round(terp, 2), dilution_if_ignored_pct=round(100 * (1 - terp / a.price), 1))
    elif a.kind == "buyback" and got.get("offer_price") and a.price:
        a.details["premium_pct"] = round(100 * (got["offer_price"] / a.price - 1), 1)


def desk(con: duckdb.DuckDBPyConnection, *, today: date | None = None, with_details: bool = True) -> list[Action]:
    """Every holding's actions in the window (buybacks / rights / demergers enriched from filings)."""
    fvs = None
    out = []
    for sym, name in holdings(con):
        for a in actions_for(con, sym, name, today=today):
            if with_details and a.kind in ("buyback", "rights", "demerger"):
                if fvs is None and a.kind == "rights":
                    fvs = ca._face_values(con)
                try:
                    enrich(con, a, fvs)
                except Exception:  # noqa: BLE001
                    log.exception("ca desk: couldn't enrich %s %s", sym, a.kind)
            out.append(a)
    return sorted(out, key=lambda a: (not a.needs_action, a.ex_date or date.today()))


SOON_DAYS = 3        # a decision-needed action gets one reminder this close to its ex-date


def fresh(con: duckdb.DuckDBPyConnection, actions: list[Action], *, today: date | None = None) -> list[Action]:
    """What the daily push should mention: actions never emailed before, plus one reminder for a
    decision-needed action whose ex-date is <= ``SOON_DAYS`` away. Marks them as sent."""
    import json

    from equity_research import scan

    today = today or date.today()
    seen = set(json.loads(scan._meta(con, "ca_desk_seen") or "[]"))
    out, keys = [], []
    for a in actions:
        soon = a.needs_action and a.ex_date is not None and 0 <= (a.ex_date - today).days <= SOON_DAYS
        for k in [a.key] + ([a.key + "|soon"] if soon else []):
            if k not in seen:
                keys.append(k)
                if a not in out:
                    out.append(a)
    if keys:
        scan._set_meta(con, "ca_desk_seen", json.dumps(sorted(seen | set(keys))))
    return out
