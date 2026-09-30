"""🔍 Reality Check — is that reel / post / article about a stock true, and does it matter?

Pipeline (the LLM reads and judges text; every number is computed here):

1. **Read** the post (``scrapers.post_fetch``) — article, X post, Reddit post or pasted text.
2. **Extract** its claims (``synthesize.reality_extract``): each claim, the companies it names, any ₹
   amount, the post's direction and its promotional phrases.
3. **Resolve** every named company to a real NSE symbol (ticker + name consistency, then the normal
   name resolver); anything that doesn't resolve is listed, not guessed.
4. **Evidence** per company: its exchange filings from the last ~90 days (numbered F1…, links kept)
   and figures computed from its reported financials (numbered C1…).
5. **Judge** each claim against that evidence only (``synthesize.reality_verify``) — confirmed / partly /
   contradicted / not found / unverifiable; a verdict citing evidence that doesn't exist is dropped.
6. **Size** it: a stated ₹ amount as a % of the company's trailing-12-month revenue and of its market cap.
7. **Price**: how far the stock has already moved vs the Nifty 500 (5 and 20 sessions, and since the
   post if its date is known), and volume vs its usual.
8. **Red flags**, computed: micro-cap, thin trading, a big run-up, a volume spike, heavy promoter pledge,
   promoter selling, promotional language, no filing behind a company-specific claim.
9. **Bottom line** by fixed rules (``verdict``), so the same evidence always gives the same answer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd

from equity_research.analysis import fundamentals, supply_chain, valuation
from equity_research.reports import synthesize
from equity_research.scrapers.post_fetch import Post, fetch_post, parse_date

log = logging.getLogger(__name__)
CR = 1e7
BENCHMARK = "Nifty 500"

MICRO_CAP_CR = 1_000          # below this market cap, a stock is easy to move
THIN_TURNOVER_CR = 1.0        # average daily traded value below this = thin
RUN_UP_PCT = 15.0             # already beaten the market by this much in 20 sessions
VOLUME_SPIKE_X = 3.0          # last 5 sessions' volume vs the usual (60 sessions)
PLEDGE_PCT = 25.0             # % of the promoter's holding pledged
MATERIAL_REV_PCT = 5.0        # a stated amount this share of annual revenue is material
MATERIAL_MCAP_PCT = 2.0       # …or this share of market cap
PRICED_IN_PCT = 10.0          # moved this much vs the market since the news → largely priced in
FILING_WINDOW_DAYS = 90
MAX_FILING_PDFS = 4           # filings read in full (the one-line listing omits amounts)
# which filings could settle which kind of claim (matched against the filing's subject)
_KIND_WORDS = {"order": ("order", "contract", "bagging", "award", "letter of"),
               "results": ("result", "financial", "outcome of board"),
               "guidance": ("investor", "presentation", "earnings call", "transcript", "analyst"),
               "capacity": ("capacity", "commission", "plant", "expansion", "capex"),
               "deal": ("acqui", "merger", "amalgamat", "agreement", "joint venture", "stake", "allot"),
               "management": ("appoint", "resign", "cessation", "director", "ceo", "cfo"),
               "policy": ("approval", "licen", "government", "regulat")}
_HYPE = re.compile(r"multi[\s-]?bagger|sure[\s-]?shot|guarantee|jackpot|rocket|to the moon|10x|\bdouble\b|"
                   r"don'?t miss|last chance|operator|insider tip|100% ?(?:return|profit|sure)|"
                   r"target (?:of )?₹?\s?\d+ in \d+ ?(?:days|weeks)", re.I)


@dataclass
class Company:
    symbol: str
    name: str
    mcap_cr: float | None = None
    ttm_revenue_cr: float | None = None
    moves: dict = field(default_factory=dict)      # ex5, ex20, since, vol_x, turnover_cr
    pledge_pct: float | None = None
    promoter_sold_cr: float = 0.0
    flags: list[str] = field(default_factory=list)
    filings_checked: bool = False


@dataclass
class Result:
    post: Post
    extracted: dict | None
    companies: dict[str, Company]
    unresolved: list[str]
    claims: list[dict]                   # extracted claim + its check + sizing
    evidence: dict[str, dict]            # id → {text, url, symbol}
    also_affected: list[dict]
    hype: list[str]
    verdict: tuple[str, str]             # (label, one-line why)
    filings_available: bool


# ------------------------------------------------------------------ resolving names
def _resolve(con: duckdb.DuckDBPyConnection, name: str, ticker: str = "") -> tuple[str, str] | None:
    hit = supply_chain._verify(con, name, (ticker or "").upper())
    if hit:
        return hit["symbol"], hit["name"]
    from equity_research.reports.resolve import resolve

    try:
        cands = resolve(name, con)
    except Exception:  # noqa: BLE001
        return None
    if len(cands) == 1:
        return cands[0].symbol, cands[0].name
    return None                                   # ambiguous (a group name) or unknown — not guessed


# ------------------------------------------------------------------ computed facts
def _moves(con, symbol: str, since: date | None) -> dict:
    px = con.execute(
        """SELECT trade_date, close, ttl_trd_qnty AS vol, turnover_lacs FROM equity_eod_adj
           WHERE symbol = ? AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
           QUALIFY row_number() OVER (PARTITION BY trade_date
                                      ORDER BY CASE series WHEN 'EQ' THEN 0 ELSE 1 END) = 1
           ORDER BY trade_date""", [symbol]).df()
    ix = con.execute("SELECT trade_date, close FROM index_close WHERE index_name = ? ORDER BY trade_date",
                     [BENCHMARK]).df()
    if len(px) < 6 or ix.empty:
        return {}
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.date
    ix["trade_date"] = pd.to_datetime(ix["trade_date"]).dt.date
    m = px.merge(ix.rename(columns={"close": "bench"}), on="trade_date", how="inner")
    out: dict = {"last_date": m["trade_date"].iloc[-1], "last": float(m["close"].iloc[-1])}

    def excess(back: int):
        if len(m) <= back:
            return None
        a, b = m.iloc[-1 - back], m.iloc[-1]
        return 100 * (b["close"] / a["close"] - 1) - 100 * (b["bench"] / a["bench"] - 1)
    out["ex5"], out["ex20"] = excess(5), excess(20)
    if since:
        before = m[m["trade_date"] < since]
        if not before.empty:
            a, b = before.iloc[-1], m.iloc[-1]
            out["since"] = 100 * (b["close"] / a["close"] - 1) - 100 * (b["bench"] / a["bench"] - 1)
            out["since_date"] = a["trade_date"]
    usual = px["vol"].iloc[-65:-5].mean() if len(px) > 10 else None
    out["vol_x"] = float(px["vol"].iloc[-5:].mean() / usual) if usual else None
    out["turnover_cr"] = float(px["turnover_lacs"].iloc[-20:].mean() / 100)
    return out


def _quarter_fact(con, symbol: str) -> str | None:
    """'Latest quarter (Jun-2026): revenue ₹X cr (+Y% YoY), net profit ₹Z cr (+W% YoY)'."""
    for cons in (True, False):
        q = fundamentals.load_quarters(con, symbol, cons)
        if len(q) >= 5 and "RevenueFromOperations" in q:
            r = q["RevenueFromOperations"]
            p = q.get("ProfitLossForPeriod", pd.Series(dtype=float))
            last, yago = q.index[-1], q.index[-5]

            def g(s):
                try:
                    return 100 * (s[last] / s[yago] - 1) if s[yago] else None
                except (KeyError, TypeError):
                    return None
            rg, pg = g(r), g(p)
            txt = f"Latest reported quarter ({pd.Timestamp(last):%b-%Y}): revenue ₹{r[last] / CR:,.0f} cr"
            txt += f" ({rg:+.0f}% YoY)" if rg is not None else ""
            if last in p.index and p[last] == p[last]:
                txt += f", net profit ₹{p[last] / CR:,.0f} cr" + (f" ({pg:+.0f}% YoY)" if pg is not None else "")
            return txt + (" [consolidated]" if cons else " [standalone]")
    return None


def _facts(con, co: Company, since: date | None) -> list[str]:
    from equity_research.reports.pipeline import ensure_ingested

    try:
        ensure_ingested(co.symbol, con)
    except Exception:  # noqa: BLE001 — work with whatever is on file
        log.info("reality check: couldn't refresh %s", co.symbol)
    snap = valuation.snapshot(con, co.symbol) or {}
    co.mcap_cr = snap.get("market_cap_cr")
    t = fundamentals.ttm(con, co.symbol, True) or fundamentals.ttm(con, co.symbol) or {}
    co.ttm_revenue_cr = t.get("ttm_revenue_cr")
    if not co.ttm_revenue_cr:                       # too few quarters on file → the last full year
        for cons in (True, False):
            a = fundamentals.load_annual(con, co.symbol, cons)
            if not a.empty and "RevenueFromOperations" in a and a["RevenueFromOperations"].iloc[-1] > 0:
                co.ttm_revenue_cr = float(a["RevenueFromOperations"].iloc[-1]) / CR
                break
    co.moves = _moves(con, co.symbol, since)
    pl = con.execute("SELECT pledged_pct_of_promoter FROM shareholding WHERE symbol = ? "
                     "ORDER BY period_end DESC LIMIT 1", [co.symbol]).fetchone()
    co.pledge_pct = pl[0] if pl else None
    sold = con.execute(
        """SELECT coalesce(sum(value_cr), 0) FROM insider_trades
           WHERE symbol = ? AND category ILIKE 'promoter%' AND (txn_type ILIKE 'sell%' OR mode ILIKE '%sale%')
             AND try_strptime(disclosure_dt, '%d-%b-%Y %H:%M') >= now() - INTERVAL 90 DAY""",
        [co.symbol]).fetchone()
    co.promoter_sold_cr = float(sold[0] or 0)
    facts = []
    if co.mcap_cr:
        facts.append(f"Market cap ₹{co.mcap_cr:,.0f} cr")
    if co.ttm_revenue_cr:
        facts.append(f"Revenue over the last 12 months ₹{co.ttm_revenue_cr:,.0f} cr")
    q = _quarter_fact(con, co.symbol)
    if q:
        facts.append(q)
    return facts


def _filings(symbol: str) -> list[dict] | None:
    """The company's exchange filings from the last ~60 days, or None when NSE access is off / fails."""
    from equity_research.scrapers import nse_api

    try:
        rows = nse_api.corporate_announcements(symbol=symbol)
    except Exception:  # noqa: BLE001 — NseScrapingDisabled or a fetch failure
        return None
    out, cutoff = [], datetime.now() - timedelta(days=FILING_WINDOW_DAYS)
    for r in rows if isinstance(rows, list) else []:
        try:
            when = datetime.strptime(str(r.get("an_dt", ""))[:20].strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            when = None
        if when and when < cutoff:
            continue
        text = " — ".join(x for x in (r.get("desc"), r.get("attchmntText")) if x)
        out.append({"date": when, "text": " ".join(str(text).split()), "url": r.get("attchmntFile") or ""})
    return out


def _flags(co: Company, hype: list[str]) -> list[str]:
    f = []
    if co.mcap_cr and co.mcap_cr < MICRO_CAP_CR:
        f.append(f"Micro-cap (₹{co.mcap_cr:,.0f} cr) — small money can move it")
    tv = co.moves.get("turnover_cr")
    if tv is not None and tv < THIN_TURNOVER_CR:
        f.append(f"Thinly traded (~₹{tv:.2f} cr a day) — hard to exit")
    ex20 = co.moves.get("ex20")
    if ex20 is not None and ex20 >= RUN_UP_PCT:
        f.append(f"Already {ex20:+.0f}% vs the market in 20 sessions")
    vx = co.moves.get("vol_x")
    if vx and vx >= VOLUME_SPIKE_X:
        f.append(f"Volume {vx:.1f}× its usual in the last week")
    if co.pledge_pct and co.pledge_pct >= PLEDGE_PCT:
        f.append(f"{co.pledge_pct:.0f}% of the promoter's shares are pledged")
    if co.promoter_sold_cr >= 1:
        f.append(f"Promoters sold ~₹{co.promoter_sold_cr:,.0f} cr in the last 90 days")
    return f


# ------------------------------------------------------------------ verdict
def verdict(claims: list[dict], companies: dict[str, Company], hype: list[str],
            filings_available: bool) -> tuple[str, str]:
    """The bottom line, by fixed rules over the checked claims and computed facts."""
    company_claims = [c for c in claims if c["symbols"]]
    if not company_claims:
        return ("ℹ️ No specific stock claim", "The post doesn't make a checkable claim about a listed company.")
    if any(c["status"] == "contradicted" for c in company_claims):
        return ("❌ Contradicted", "The company's own filings or reported numbers say otherwise.")
    confirmed = [c for c in company_claims if c["status"] == "confirmed"]
    partly = [c for c in company_claims if c["status"] == "partly"]
    flags = sum(len(companies[s].flags) for s in {s for c in company_claims for s in c["symbols"]})
    if partly and not confirmed:
        return ("🟡 Partly true", "The event is on record, but the post's details don't match what was disclosed.")
    if confirmed:
        sized = [c for c in confirmed if c.get("pct_rev") is not None or c.get("pct_mcap") is not None]
        material = any((c.get("pct_rev") or 0) >= MATERIAL_REV_PCT or (c.get("pct_mcap") or 0) >= MATERIAL_MCAP_PCT
                       for c in sized)
        moved = max((abs(v) for s in {s for c in confirmed for s in c["symbols"]}
                     for v in (companies[s].moves.get("since"), companies[s].moves.get("ex20")) if v is not None),
                    default=0)
        if sized and not material:
            return ("🟢 Real, but small", "It's on record, but small next to the company's size.")
        if material and moved >= PRICED_IN_PCT:
            return ("🟡 Real — but the stock has already moved",
                    f"On record and material, and the stock has already moved ~{moved:.0f}% vs the market.")
        if material:
            return ("✅ Real and material", "On record, and large relative to the company.")
        return ("✅ Confirmed — size not stated", "On record; the post gives no amount to size it.")
    if hype or flags >= 3:
        return ("🚩 Looks like hype", "Nothing on record backs it, and it carries promotional language or red flags.")
    if not filings_available:
        return ("⚠️ Unconfirmed", "Exchange filings weren't checked (NSE access is off), so nothing could be confirmed.")
    return ("⚠️ Unconfirmed", "No exchange filing or reported number backs it yet.")


def _filing_pdfs(claims: list[dict], evidence: dict[str, dict]) -> list[tuple[str, bytes]]:
    """The few filing PDFs most likely to settle the claims (subject matches the claim's kind, most
    recent first), fetched for the model to read. Best-effort."""
    from equity_research.common.http import fetch_bytes

    want: list[str] = []
    for c in claims:
        words = _KIND_WORDS.get(c.get("kind", ""), ())
        for fid, ev in evidence.items():
            if (fid.startswith("F") and ev["url"].lower().endswith(".pdf") and ev["symbol"] in c["symbols"]
                    and any(w in ev["text"].lower() for w in words) and fid not in want):
                want.append(fid)
    out = []
    for fid in sorted(want, key=lambda f: int(f[1:]))[:MAX_FILING_PDFS]:
        try:
            out.append((f"{fid}.pdf", fetch_bytes(evidence[fid]["url"])))
        except Exception:  # noqa: BLE001
            log.info("reality check: couldn't fetch %s", evidence[fid]["url"])
    return out


# ------------------------------------------------------------------ the whole check
def run(con: duckdb.DuckDBPyConnection, raw: str) -> Result:
    post = fetch_post(raw)
    empty = Result(post, None, {}, [], [], {}, [], [], ("", ""), False)
    if post.kind in ("error", "unsupported") and not post.text:
        return empty
    ex = synthesize.reality_extract(post.text)
    if not ex:
        return Result(post, None, {}, [], [], {}, [], [], ("", "couldn't read any claims"), False)
    since = parse_date(ex.get("post_date") or post.published)
    since_d = since.date() if since and since.date() <= date.today() else None

    companies: dict[str, Company] = {}
    unresolved: list[str] = []
    claims = []
    for c in ex["claims"]:
        syms = []
        for co in c.get("companies") or []:
            hit = _resolve(con, co.get("name", ""), co.get("ticker", ""))
            if not hit:
                unresolved.append(co.get("name", ""))
                continue
            companies.setdefault(hit[0], Company(hit[0], hit[1]))
            syms.append(hit[0])
        claims.append({**c, "symbols": list(dict.fromkeys(syms))})

    evidence: dict[str, dict] = {}
    filings_available = False
    nf = nc = 0
    for sym, co in companies.items():
        for fact in _facts(con, co, since_d):
            nc += 1
            evidence[f"C{nc}"] = {"text": f"[{sym}] {fact}", "url": "", "symbol": sym}
        fl = _filings(sym)
        if fl is not None:
            filings_available = co.filings_checked = True
            for f in fl:
                nf += 1
                when = f"{f['date']:%d-%b-%Y}" if f["date"] else ""
                evidence[f"F{nf}"] = {"text": f"[{sym}] {when} {f['text']}", "url": f["url"], "symbol": sym}

    pdfs = _filing_pdfs(claims, evidence)
    log.info("reality check: %d claims, %d evidence items, %d filing PDFs read", len(claims), len(evidence), len(pdfs))
    checks = synthesize.reality_verify(claims, {k: v["text"] for k, v in evidence.items()}, files=pdfs)
    for c, chk in zip(claims, checks):
        c.update(status=chk["status"], evidence_id=chk["evidence"], note=chk["note"],
                 evidence_url=evidence.get(chk["evidence"], {}).get("url", ""))
        amt = c.get("amount_cr")
        if isinstance(amt, (int, float)) and amt > 0 and c["symbols"]:
            co = companies[c["symbols"][0]]
            c["pct_rev"] = 100 * amt / co.ttm_revenue_cr if co.ttm_revenue_cr else None
            c["pct_mcap"] = 100 * amt / co.mcap_cr if co.mcap_cr else None

    hype = list(dict.fromkeys([*ex.get("hype_phrases", []), *(m.group(0) for m in _HYPE.finditer(post.text))]))
    for co in companies.values():
        co.flags = _flags(co, hype)
        if co.filings_checked and any(c["status"] == "not_found" and co.symbol in c["symbols"] for c in claims):
            co.flags.append("A company-specific claim with no exchange filing behind it")

    also = []
    for a in ex.get("also_affected", []):
        hit = supply_chain._verify(con, a.get("name", ""), (a.get("ticker") or "").upper())
        if hit and hit["symbol"] not in companies:
            also.append({"symbol": hit["symbol"], "name": hit["name"], "why": a.get("why", "")})

    return Result(post, ex, companies, [u for u in dict.fromkeys(unresolved) if u], claims, evidence, also,
                  hype, verdict(claims, companies, hype, filings_available), filings_available)
