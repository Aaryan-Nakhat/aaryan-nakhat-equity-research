"""Resolve a free-text company name/query to NSE trading symbol candidates.

Two layers:

1. **Deterministic, from the local listed-company master** (``equity_master``) — no LLM:
   - the query IS an NSE symbol (``INFY``, ``SBIN``, ``BAJAJ-AUTO``) → that one company;
   - the query's words start exactly one listed company's name ("state bank", "hdfc bank",
     "larsen") → that company;
   - they start SEVERAL names — a group/brand like "hdfc", "tata", "adani" → always a numbered
     list, never a silent guess. The LLM's pick (if any) goes first, the rest by trading value.
2. **LLM + web search** for everything else (typos, short forms, "hdfc amc", recent listings) —
   it works for any listed name, including small caps not yet in the local master.

Every symbol the LLM suggests is **checked against the live master**: a renamed symbol is followed
to its current one via NSE's official symbol-change list (ZOMATO → ETERNAL, L&TFH → LTF,
TATAMOTORS → TMPV), and a delisted one (ISEC, TATAMTRDVR) is dropped — a stale ticker would
otherwise produce an empty report.

Returns ≤ ``_MAX_CANDIDATES`` ranked candidates, or exactly one when the match is certain. The
caller disambiguates when there's more than one. The local layer is best-effort: if the database
can't be read, resolution falls back to the LLM alone.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from equity_research.common import llm

log = logging.getLogger(__name__)

_MAX_CANDIDATES = 8          # a group name can match many (Tata → 13); show the most-traded ones
_MIN_MASTER = 1000           # below this the master looks unpopulated → don't filter LLM picks by it
_RENAMES_TTL_S = 24 * 3600
_renames_cache: tuple[float, dict[str, str]] | None = None
_renames_lock = threading.Lock()

_RESOLVER_SYS = (
    "You map an Indian company name/query to its stock trading symbol(s) on NSE "
    "(preferred) or BSE. Use web search to find the correct tickers, including "
    "small-cap and recently-listed companies. Return the best matches, RANKED by "
    "relevance, as a JSON array of objects {\"symbol\":..., \"name\":..., "
    "\"exchange\":\"NSE\"|\"BSE\"}. The symbol must be the exact NSE trading symbol "
    "(e.g. RELIANCE, TCS, INFY). If you are fully sure of a single "
    "match, return exactly one element; otherwise return up to 5 plausible "
    "candidates. If the query is a group or brand name shared by several listed companies "
    "(e.g. 'HDFC', 'Tata', 'Bajaj', 'Adani'), return all of them — never pick just one. "
    "Reply with ONLY the JSON array, no prose."
)

_SUFFIX = re.compile(r"\s+(?:ltd|limited|co|company|corp|corporation|inc)$")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    exchange: str = "NSE"


def _norm(s: str) -> str:
    """Lowercase, punctuation → space (keeping '&' and '-'), collapse spaces, drop 'Ltd'-style tails."""
    s = re.sub(r"[^\w&\- ]+", " ", (s or "").lower())
    s = " ".join(s.split())
    while True:
        t = _SUFFIX.sub("", s)
        if t == s:
            return s
        s = t


def _llm_candidates(query: str) -> list[Candidate]:
    try:
        text = llm.generate(_RESOLVER_SYS, query, grounded=True)
    except Exception:  # noqa: BLE001
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)   # strip ```json fences / prose
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Candidate] = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        sym, name = d.get("symbol"), d.get("name")
        if sym and str(d.get("exchange", "NSE")).upper() == "NSE":   # analysis = NSE only
            out.append(Candidate(str(sym).upper(), name or str(sym), "NSE"))
    return out


def _renames() -> dict[str, str]:
    """NSE's official ``{old: new}`` symbol changes, cached for a day. Best-effort: {} if unreachable."""
    global _renames_cache
    with _renames_lock:
        if _renames_cache and time.monotonic() - _renames_cache[0] < _RENAMES_TTL_S:
            return _renames_cache[1]
        try:
            from equity_research.scrapers.nse_archives import fetch_symbol_changes
            data = fetch_symbol_changes()
        except Exception:  # noqa: BLE001 — renames are a refinement; resolution works without them
            log.warning("NSE symbol-change list unavailable — renamed tickers won't be remapped",
                        exc_info=True)
            data = _renames_cache[1] if _renames_cache else {}
        _renames_cache = (time.monotonic(), data)
        return data


def _current(sym: str, master: dict[str, str]) -> str | None:
    """``sym`` as it trades today: itself if listed, else followed through NSE renames (a chain like
    TELCO → TATAMOTORS → TMPV); None if it leads nowhere listed (delisted / merged away)."""
    s = sym.upper()
    if s in master:
        return s
    renames = _renames()
    for _ in range(6):
        s = renames.get(s, "")
        if not s:
            return None
        if s in master:
            return s
    return None


def _validate(cands: list[Candidate], master: dict[str, str]) -> list[Candidate]:
    """Map LLM picks onto live symbols (following renames), drop dead ones, dedup — order kept."""
    if len(master) < _MIN_MASTER:
        return cands
    out: list[Candidate] = []
    seen: set[str] = set()
    for c in cands:
        cur = _current(c.symbol, master)
        if cur is None:
            log.info("resolve: dropping %s — not a listed NSE symbol (delisted/merged?)", c.symbol)
        elif cur not in seen:
            seen.add(cur)
            out.append(Candidate(cur, master[cur] or c.name))
    return out


def _master(con) -> dict[str, str]:
    return {sym.upper(): name or sym for sym, name in
            con.execute("SELECT symbol, company_name FROM equity_master").fetchall() if sym}


def _local(master: dict[str, str], query: str) -> tuple[Candidate | None, list[Candidate]]:
    """(exact-symbol hit — a current or renamed symbol, companies whose normalised name starts
    with the query's words)."""
    q = _norm(query)
    if not q:
        return None, []
    sym_q = q.replace(" ", "").upper()
    if sym_q in master:
        return Candidate(sym_q, master[sym_q]), []
    hits = [Candidate(sym, name) for sym, name in master.items()
            if name and (_norm(name) == q or _norm(name).startswith(q + " "))]
    if not hits and len(master) >= _MIN_MASTER and re.fullmatch(r"[A-Z0-9&\-]{2,20}", sym_q):
        cur = _current(sym_q, master)             # an old ticker you remember ('zomato', 'l&tfh')
        if cur:
            return Candidate(cur, master[cur]), []
    return None, hits


def _by_trading_value(con, cands: list[Candidate]) -> list[Candidate]:
    """Most-traded first (avg daily ₹ value over the last ~month) — a proxy for 'the one you meant'."""
    if len(cands) < 2:
        return cands
    syms = [c.symbol for c in cands]
    try:
        val = dict(con.execute(
            f"""SELECT symbol, avg(close * ttl_trd_qnty) FROM equity_eod
                WHERE symbol IN ({",".join("?" * len(syms))})
                  AND trade_date >= (SELECT max(trade_date) FROM equity_eod) - INTERVAL 30 DAY
                GROUP BY 1""", syms).fetchall())
    except Exception:  # noqa: BLE001 — ranking is cosmetic
        return cands
    return sorted(cands, key=lambda c: -(val.get(c.symbol) or 0))


def resolve(query: str, con=None) -> list[Candidate]:
    """NSE symbol candidates for ``query`` — deterministic local match first, else LLM + web search."""
    own = con is None
    master: dict[str, str] = {}
    try:
        if own:
            from equity_research.common.db import connect
            con = connect()
        master = _master(con)
        exact, local = _local(master, query)
        if exact:
            return [exact]
        if len(local) == 1:
            return local
        if len(local) >= 2:
            # A group/brand name: always ask. The LLM's pick leads (it can also add a name a prefix
            # can't see, e.g. SBIN for "sbi"); the remaining local matches follow by trading value.
            ranked = _validate(_llm_candidates(query), master) + _by_trading_value(con, local)
            seen: set[str] = set()
            out: list[Candidate] = []
            for c in ranked:
                if c.symbol not in seen:
                    seen.add(c.symbol)
                    out.append(c)
            return out[:_MAX_CANDIDATES]
    except Exception:  # noqa: BLE001 — the local layer is an accelerator, never a blocker
        log.warning("local name match unavailable for %r — using the LLM resolver", query,
                    exc_info=True)
    finally:
        if own and con is not None:
            con.close()
    return _validate(_llm_candidates(query), master)[:5]
