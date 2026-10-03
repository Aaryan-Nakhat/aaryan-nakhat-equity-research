"""Splits, bonuses, consolidations, rights and demergers → price adjustments (``price_adjustments``).

NSE's bhavcopy is *unadjusted*: on a 1:5 split's ex-date the price simply drops ~80%. Left alone
that breaks everything that compares prices across time — moving averages, 52-week highs,
3/6/12-month returns, smart-money cost zones. This module records one factor per action; the
``equity_eod_adj`` view (``common/db.py``) applies them.

Sources, most-authoritative first:

1. **NSE's corporate-action record** (``nse_api``, the browser tier — only when
   ``NSE_SCRAPING_ENABLED``). Splits, bonuses and consolidations carry an exact ratio in the
   subject ("Bonus 1:1", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per
   Share"). A **rights issue** is sized by the textbook theoretical ex-rights price — (old shares
   × previous close + new shares × issue price) ÷ all shares, the issue price being face value +
   the premium in the subject. A **demerger** carries no ratio — its size is what the market
   discovers: NSE runs a special pre-open session on the ex-date, so factor = that session's open
   ÷ the previous close.
   Records are also filed under a company's *old* symbols (NSE's symbol-change list), because the
   price rows around an ex-date can predate a rename.
2. **The bhavcopy itself** (always available): a session whose opening price sits at a standard
   split/bonus ratio of the *same file's* previous close, and whose close confirms it. Used for
   ETFs (unit splits aren't in the corporate-action feed; NSE's ETF list tells them apart) and,
   when NSE's record isn't available, for stocks too. When NSE's record *is* available, a stock
   gap it doesn't explain is recorded as ``unexplained`` and left unadjusted — never guessed.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta

import duckdb

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- NSE subjects → factor
_BONUS = re.compile(r"\bbonus\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)
_FV = re.compile(r"from\s+r[se]\.?\s*(\d+(?:\.\d+)?)\s*/?-?.*?\bto\s+r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_RIGHTS = re.compile(r"\brights?\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*@\s*premium\s*r[se]\.?\s*"
                     r"(\d+(?:\.\d+)?)", re.I)
# a bonus of preference shares / debentures doesn't change the equity share count
_NOT_EQUITY = ("ncrps", "preference", "debenture", "ncd", "warrant")


def parse_subject(subject: str) -> tuple[str, float | None] | None:
    """``(kind, factor)`` for a split / bonus / consolidation / rights / demerger subject, else None.

    factor = new price ÷ old price for the same holding: an a:b bonus (a new for every b held)
    → b/(a+b); a face-value split from F1 to F2 → F2/F1; a consolidation → F2/F1 (> 1). Rights
    and demergers depend on the price — factor None here, sized from the bhavcopy later."""
    s = " ".join(str(subject or "").split())
    low = s.lower()
    if not s or any(k in low for k in _NOT_EQUITY):
        return None
    parts: list[tuple[str, float]] = []
    if (m := _BONUS.search(s)):
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            parts.append(("bonus", b / (a + b)))
    if any(k in low for k in ("split", "sub-division", "subdivision", "consolidation")):
        if (m := _FV.search(s)):
            old, new = float(m.group(1)), float(m.group(2))
            if old > 0 and new > 0 and old != new:
                parts.append(("consolidation" if new > old else "split", new / old))
    if parts:
        return "+".join(k for k, _ in parts), math.prod(f for _, f in parts)
    if _RIGHTS.search(s):
        return "rights", None
    if "demerger" in low:
        return "demerger", None
    return None


def _parse_date(s) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _upsert(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    """Rows are ``(symbol, ex_date, factor, share_mult, kind, source, detail)``."""
    if rows:
        con.executemany("INSERT OR REPLACE INTO price_adjustments VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


Part = tuple  # (kind, fixed factor or None, subject)


def nse_parts(actions, aliases: dict[str, list[str]] | None = None
              ) -> dict[tuple[str, date], tuple[str, list[Part]]]:
    """NSE corporate-action records grouped by (symbol, ex-date) → (the record's own symbol, parts)
    — pure, no I/O. Each record is also filed under the symbol's former names in ``aliases``
    ({current: [old, …]}), because the price rows around an ex-date can predate a rename; the
    caller keeps such an alias only where it fits. Duplicate records collapse."""
    out: dict[tuple[str, date], tuple[str, list[Part]]] = {}
    for r in actions if isinstance(actions, list) else []:
        r = r or {}
        parsed = parse_subject(r.get("subject"))
        ex = _parse_date(r.get("exDate"))
        sym = str(r.get("symbol") or "").strip().upper()
        if not parsed or not ex or not sym:
            continue
        subject = " ".join(str(r.get("subject")).split())
        for s in [sym, *(aliases or {}).get(sym, [])]:
            _, parts = out.setdefault((s, ex), (sym, []))
            same = any(p[2].lower() == subject.lower()
                       or (p[0] == parsed[0] and p[1] is not None and p[1] == parsed[1]) for p in parts)
            if not same:                         # the same action filed twice under different wording counts once
                parts.append((parsed[0], parsed[1], subject))
    return out


def size_parts(parts: list[Part], session: tuple[date, float, float] | None,
               face_value: float | None) -> tuple[float | None, float | None, str]:
    """(price factor, share multiplier, how) for one ex-date's actions — pure.

    ``session`` is the ex-date's (date, previous close, open) from the bhavcopy. Splits, bonuses and
    consolidations are exact. Rights use the theoretical ex-rights price (issue price = face value
    + premium; fully-subscribed share count). A demerger — alone or with anything else that day — is
    sized by the discovered opening price, which already reflects every action of the session."""
    fixed = [f for _, f, _ in parts if f is not None]
    share_mult: float | None = math.prod(1 / f for f in fixed) if fixed else 1.0
    if any("demerger" in k for k, _, _ in parts):
        if not session:
            return None, share_mult, "no session on file around the ex-date — not sized"
        d, prev, opn = session
        f = opn / prev
        if not 0.02 < f < 0.999:
            return None, share_mult, f"no drop at the discovered open ({opn:g} vs {prev:g}) — not applied"
        return f, share_mult, f"discovered open {opn:g} vs previous close {prev:g} on {d:%d-%b-%Y}"
    factor: float | None = math.prod(fixed) if fixed else 1.0
    notes: list[str] = []
    for kind, _, subject in parts:
        if kind != "rights":
            continue
        m = _RIGHTS.search(subject)
        a, b, prem = float(m.group(1)), float(m.group(2)), float(m.group(3))
        share_mult = share_mult * (a + b) / b if share_mult is not None else None
        if not session or not face_value:
            factor = None
            notes.append("rights not sized (no " + ("session on file" if not session else "face value") + ")")
            continue
        if factor is None:
            continue
        prev = session[1] * factor                     # after any same-day split / bonus
        issue = face_value + prem
        if issue >= prev:
            notes.append(f"rights at ₹{issue:g} ≥ market ₹{prev:g} — no price adjustment")
            continue
        terp = (b * prev + a * issue) / (a + b)
        factor *= terp / prev
        notes.append(f"rights {a:g}:{b:g} at ₹{issue:g} vs ₹{prev:g} → ex-rights ₹{terp:.2f}")
    return factor, share_mult, "; ".join(notes)


def _session(con: duckdb.DuckDBPyConnection, symbol: str, ex: date) -> tuple[date, float, float] | None:
    """The first session on/after the ex-date (within 5 days): (date, previous close, open)."""
    row = con.execute(
        """SELECT trade_date, prev_close, open FROM equity_eod
           WHERE symbol = ? AND trade_date BETWEEN ? AND ? AND prev_close > 0 AND open > 0
           ORDER BY trade_date, series LIMIT 1""", [symbol, ex, ex + timedelta(days=5)]).fetchone()
    return (row[0], row[1], row[2]) if row else None


def _face_values(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Face value per symbol: NSE's equity lists, else the latest filed value in ``financials``."""
    from equity_research.scrapers import nse_archives

    out = {r[0]: float(r[1]) for r in con.execute(
        """SELECT symbol, arg_max(value, period_end) FROM financials
           WHERE element = 'FaceValueOfEquityShareCapital' AND value > 0 GROUP BY symbol""").fetchall()}
    try:
        out.update(nse_archives.fetch_face_values())
    except Exception:  # noqa: BLE001
        log.warning("NSE equity lists unavailable — face values from filings only")
    return out


def _aliases() -> dict[str, list[str]]:
    """{current symbol: [former symbols]} from NSE's symbol-change list, following chains
    (TELCO → TATAMOTORS → TMPV gives TMPV: [TATAMOTORS, TELCO])."""
    from equity_research.scrapers import nse_archives

    try:
        changes = nse_archives.fetch_symbol_changes()          # {old: new}
    except Exception:  # noqa: BLE001
        log.warning("symbol-change list unavailable — NSE records filed under current symbols only")
        return {}
    back: dict[str, list[str]] = {}
    for old, new in changes.items():
        back.setdefault(new, []).append(old)
    out: dict[str, list[str]] = {}
    for new in back:
        seen, stack = [], list(back[new])
        while stack:
            s = stack.pop()
            if s not in seen and s != new:
                seen.append(s)
                stack += back.get(s, [])
        out[new] = seen
    return out


def ingest_nse(con: duckdb.DuckDBPyConnection, start: date, end: date) -> int:
    """Fetch NSE's split / bonus / consolidation / rights / demerger records for [start, end]
    (main board and SME, ≤1-year windows), size each ex-date against the bhavcopy and store them,
    replacing any detected gap within 5 days. Needs the NSE browser tier; returns rows written."""
    from equity_research.scrapers import nse_api

    raw: list = []
    for index in ("equities", "sme"):
        lo = start
        while lo <= end:
            hi = min(end, lo + timedelta(days=364))
            data = nse_api.fetch_api(f"/api/corporates-corporateActions?index={index}"
                                     f"&from_date={lo:%d-%m-%Y}&to_date={hi:%d-%m-%Y}")
            raw += data if isinstance(data, list) else ((data or {}).get("data") or [])
            lo = hi + timedelta(days=1)
    grouped = nse_parts(raw, _aliases())
    fvs = (_face_values(con) if any(k == "rights" for _, ps in grouped.values() for k, _, _ in ps)
           else {})
    rows = []
    for (sym, ex), (own, parts) in grouped.items():
        session = _session(con, sym, ex)
        # A former symbol can later be re-used by a different company: file the record under an
        # old name only where that name traded at the ex-date and the current one didn't.
        if sym != own and (session is None or _session(con, own, ex) is not None):
            continue
        factor, mult, how = size_parts(parts, session, fvs.get(sym))
        detail = " · ".join(p[2] for p in parts) + (f" — {how}" if how else "")
        rows.append((sym, ex, factor, mult, "+".join(p[0] for p in parts), "nse", detail))
    for sym, ex, *_ in rows:
        con.execute("DELETE FROM price_adjustments WHERE symbol = ? AND source = 'price-gap' "
                    "AND ex_date BETWEEN ? AND ?", [sym, ex - timedelta(days=5), ex + timedelta(days=5)])
    _upsert(con, rows)
    return len(rows)


# ---------------------------------------------------------------- detection from the bhavcopy
# Standard ratios: face-value splits (10→5/2/1, 5→2/1, 2→1 …), bonuses a:b → b/(a+b), and
# consolidations. Anything milder than halving (a 1:2 bonus is ×2/3) is too close to a genuine
# bad day to infer from price alone — that comes only from NSE's record.
_CANDIDATES: dict[float, str] = {
    1 / 2: "split 1:2 or bonus 1:1", 1 / 3: "bonus 2:1", 1 / 4: "bonus 3:1", 1 / 5: "split 1:5 or bonus 4:1",
    1 / 6: "bonus 5:1", 1 / 10: "split 1:10", 1 / 20: "split 1:20", 1 / 100: "split 1:100",
    2 / 5: "split 5→2 or bonus 3:2",
    2.0: "consolidation 2:1", 5.0: "consolidation 5:1", 10.0: "consolidation 10:1",
}
# ordinary shares (main board, trade-for-trade, SME) and ETFs — not bonds, InvITs/REITs or rights
_EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "SZ")
_OPEN_TOL = math.log(1.12)      # opening gap vs the ratio: within a normal overnight move
_CLOSE_TOL = math.log(1.25)     # the session's close still near the ratio (circuit limits ≤20%)
_GAP_BELOW, _GAP_ABOVE = 0.70, 1.60   # only openings this far from the previous close are candidates


def _snap(ratio: float) -> float | None:
    """The standard factor a gap unambiguously matches, else None."""
    ranked = sorted(_CANDIDATES, key=lambda f: abs(math.log(ratio / f)))
    d0 = abs(math.log(ratio / ranked[0]))
    if d0 > _OPEN_TOL:
        return None
    d1 = abs(math.log(ratio / ranked[1]))
    return ranked[0] if d1 - d0 > math.log(1.05) else None   # two ratios fit about equally → ambiguous


def detect_gaps(con: duckdb.DuckDBPyConnection, since: date | None = None, *,
                etfs: set[str] | None = None, nse_covered: bool = False) -> dict[str, int]:
    """Scan the bhavcopy for split/bonus-shaped opening gaps (against the same file's
    ``prev_close``, which NSE does not adjust) and record them, skipping any within 5 days of an
    NSE record. Only a stock that also traded in the previous 10 days counts — on a listing or
    relisting day ``prev_close`` is the issue / old price — and rights entitlements (``…-RE``),
    which collapse at expiry by design, are excluded.

    ``nse_covered``: NSE's record was available, so a *stock* gap it doesn't explain is recorded as
    ``unexplained`` rather than applied (ETFs in ``etfs`` still are — the feed doesn't carry unit
    splits). Returns counts of applied / unexplained gaps written."""
    where = "AND e.trade_date >= ?" if since else ""
    params = [since] if since else []
    gaps = con.execute(
        f"""SELECT e.symbol, e.trade_date, e.prev_close, e.open, e.close
            FROM equity_eod e
            WHERE e.prev_close > 0 AND e.open > 0 AND e.close > 0
              AND e.series IN {_EQUITY_SERIES}
              AND NOT regexp_matches(e.symbol, '-RE[0-9]*$')
              AND (e.open / e.prev_close < {_GAP_BELOW} OR e.open / e.prev_close > {_GAP_ABOVE})
              {where}
              AND EXISTS (SELECT 1 FROM equity_eod y
                          WHERE y.symbol = e.symbol AND y.trade_date < e.trade_date
                            AND y.trade_date >= e.trade_date - INTERVAL 10 DAY)
              AND NOT EXISTS (SELECT 1 FROM price_adjustments p
                              WHERE p.symbol = e.symbol AND p.source = 'nse'
                                AND p.ex_date BETWEEN e.trade_date - INTERVAL 5 DAY
                                                  AND e.trade_date + INTERVAL 5 DAY)
            ORDER BY e.symbol, e.trade_date""", params).fetchall()
    rows, applied, unexplained = [], 0, 0
    seen: set[tuple[str, date]] = set()
    for sym, d, prev, opn, close in gaps:
        if (sym, d) in seen:                  # the same symbol in two series on one day
            continue
        seen.add((sym, d))
        gap = f"open {opn:g} vs previous close {prev:g}"
        is_etf = sym in (etfs or set())
        f = _snap(opn / prev)
        fits = f is not None and abs(math.log((close / prev) / f)) <= _CLOSE_TOL
        if fits and (is_etf or not nse_covered):
            note = ("ETF unit split" if is_etf else
                    "standard ratio — could also be a demerger; NSE's record gives the exact action")
            rows.append((sym, d, f, 1 / f, "consolidation" if f > 1 else "split/bonus", "price-gap",
                         f"{gap} → {_CANDIDATES[f]} ({note})"))
            applied += 1
        else:
            why = ("no split, bonus or demerger on NSE's record" if fits
                   else f"×{opn / prev:.3f} matches no standard ratio")
            rows.append((sym, d, None, None, "unexplained", "price-gap", f"{gap} — {why}; left unadjusted"))
            unexplained += 1
    _upsert(con, rows)
    return {"applied": applied, "unexplained": unexplained}


def repair_phantom_sessions(con: duckdb.DuckDBPyConnection) -> list[date]:
    """Delete bhavcopy rows stored under a market holiday — copies of the previous session's file
    that NSE serves on a holiday URL (ingest now rejects them; this cleans up what was stored
    before). A date is a phantom when essentially every row repeats the previous session exactly."""
    days = [r[0] for r in con.execute(
        """WITH x AS (
               SELECT trade_date, open, high, low, close, ttl_trd_qnty,
                      lag(open) OVER w AS po, lag(high) OVER w AS ph, lag(low) OVER w AS pl,
                      lag(close) OVER w AS pc, lag(ttl_trd_qnty) OVER w AS pv,
                      lag(trade_date) OVER w AS pd
               FROM equity_eod WHERE series = 'EQ'
               WINDOW w AS (PARTITION BY symbol, series ORDER BY trade_date))
           SELECT trade_date FROM x WHERE pd IS NOT NULL
           GROUP BY trade_date
           HAVING count(*) >= 200
              AND avg(CASE WHEN open = po AND high = ph AND low = pl AND close = pc
                            AND ttl_trd_qnty = pv THEN 1 ELSE 0 END) >= 0.99
           ORDER BY trade_date""").fetchall()]
    for d in days:
        con.execute("DELETE FROM equity_eod WHERE trade_date = ?", [d])
    if days:
        log.info("removed %d phantom holiday session(s) from equity_eod: %s", len(days),
                 ", ".join(map(str, days)))
    return days


# ---------------------------------------------------------------- orchestration + reads
def refresh(con: duckdb.DuckDBPyConnection, *, since: date | None = None,
            nse: bool | None = None) -> dict:
    """Bring ``price_adjustments`` up to date. ``since=None`` scans the whole history (first run,
    which also removes phantom holiday sessions); otherwise only sessions from ``since``. NSE
    records are fetched when the NSE tier is enabled (``nse`` overrides). Never raises on a fetch
    failure — detection still runs."""
    from equity_research.scrapers import nse_api, nse_archives

    out: dict = {}
    if since is None:
        out["phantom_sessions_removed"] = len(repair_phantom_sessions(con))
    use_nse = nse_api._nse_scraping_enabled() if nse is None else nse
    if use_nse:
        start = since or (con.execute("SELECT min(trade_date) FROM equity_eod").fetchone()[0]
                          or date.today())
        try:
            out["nse"] = ingest_nse(con, start - timedelta(days=5), date.today() + timedelta(days=30))
        except Exception:  # noqa: BLE001
            log.exception("NSE corporate-action fetch failed — using price-gap detection only")
    nse_covered = con.execute(
        "SELECT count(*) FROM price_adjustments WHERE source = 'nse'").fetchone()[0] > 0
    try:
        etfs = nse_archives.fetch_etf_symbols()
    except Exception:  # noqa: BLE001
        log.warning("NSE ETF list unavailable — ETF unit splits need it when NSE records are on")
        etfs = set()
    out.update(detect_gaps(con, since, etfs=etfs, nse_covered=nse_covered))
    return out


def share_multiplier_since(con: duckdb.DuckDBPyConnection, symbol: str, after: date,
                           until: date | None = None) -> tuple[float, list[str], list[date]]:
    """How many times the share count has multiplied through ``symbol``'s actions with ex-date in
    (after, until] (splits, bonuses, consolidations, rights; a demerger leaves it alone), and what
    they were (labels, ex-dates) — to bring a share count filed at a year-end up to today. An action
    whose effect on the count is unknown contributes nothing and isn't listed."""
    rows = con.execute(
        """SELECT share_mult, ex_date, kind FROM price_adjustments
           WHERE symbol = ? AND share_mult IS NOT NULL AND share_mult <> 1 AND ex_date > ?
             AND ex_date <= coalesce(?, (SELECT max(trade_date) FROM equity_eod))
           ORDER BY ex_date""", [symbol, after, until]).fetchall()
    return (math.prod(r[0] for r in rows),
            [f"{r[2]} (ex {r[1]:%d-%b-%Y}, ×{r[0]:.3g} shares)" for r in rows],
            [r[1] for r in rows])


def maybe_refresh(con: duckdb.DuckDBPyConnection) -> dict:
    """Full refresh the first time (empty table), else the last ~10 days. Cheap enough to call
    after every daily ingest."""
    empty = con.execute("SELECT count(*) FROM price_adjustments").fetchone()[0] == 0
    return refresh(con, since=None if empty else date.today() - timedelta(days=10))
