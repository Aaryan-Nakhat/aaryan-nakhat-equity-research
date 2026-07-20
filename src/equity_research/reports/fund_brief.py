"""Mutual-fund deep-brief — the fund-side analogue of ``deep_brief`` for stocks.

Resolves a free-text fund name to an AMFI scheme (preferring the Direct-Growth share
class), backfills its NAV history on demand via the AMC-code map, and renders a
returns + risk report from ``analysis.funds``. Holdings look-through and the forensic
portfolio-quality score are layered in when ``mf_holdings`` coverage exists (Phase 3/5).
"""

from __future__ import annotations

import re

import duckdb

from equity_research.analysis import funds
from equity_research.ingest import backfill_mf_scheme_history

# share classes people actually research; a bare name resolves to Direct-Growth
# (exclude bonus/institutional/direct-payout variants that also parse as 'Growth')
_CANON = ("plan = 'Direct' AND (option = 'Growth' OR option IS NULL) "
          "AND scheme_name NOT ILIKE '%bonus%' AND scheme_name NOT ILIKE '%institutional%'")


# share-class / filler words to drop from a query (share class is handled by _CANON)
_FUND_STOP = {"fund", "plan", "the", "direct", "regular", "growth", "idcw", "dividend",
              "option", "scheme", "of", "an"}


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _FUND_STOP]


def _family_key(name: str) -> str:
    """Collapse plan/option variants so 'X - Direct Growth' and 'X - Regular IDCW' dedupe."""
    n = name.lower()
    for junk in ("direct", "regular", "growth", "idcw", "dividend", "plan", "option",
                 "payout", "reinvestment", "-"):
        n = n.replace(junk, " ")
    return re.sub(r"\s+", " ", n).strip()


def resolve_fund(con: duckdb.DuckDBPyConnection, query: str, limit: int = 5) -> list[tuple[int, str]]:
    """Best fund-scheme matches for a free-text name → ``[(scheme_code, name), ...]``.

    Token-AND match on the Direct-Growth universe, ranked by tightest name, deduped to
    one row per fund family. Falls back to any plan if Direct-Growth has no hit."""
    toks = _tokens(query)
    if not toks:
        return []
    # match on a space/punctuation-collapsed name so 'midcap' hits 'Mid Cap Fund'
    col = "regexp_replace(lower(scheme_name), '[^a-z0-9]', '', 'g')"
    where = " AND ".join([f"{col} LIKE ?"] * len(toks))
    params = [f"%{t}%" for t in toks]
    for extra in (f"AND {_CANON}", ""):        # prefer canonical share class, then anything
        rows = con.execute(
            f"SELECT scheme_code, scheme_name FROM mf_scheme WHERE {where} {extra} "
            "ORDER BY length(scheme_name)", params).fetchall()
        if rows:
            break
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for code, name in rows:
        fk = _family_key(name)
        if fk in seen:
            continue
        seen.add(fk)
        out.append((code, name))
        if len(out) >= limit:
            break
    return out


def _pct(v: float | None, plus: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%" if plus else f"{v:.1f}%"


def _returns_block(r: dict) -> str:
    order = [("1m", "1-month"), ("3m", "3-month"), ("6m", "6-month"),
             ("1y", "1-year"), ("3y", "3-year CAGR"), ("5y", "5-year CAGR"), ("incep", "since-incep CAGR")]
    return "\n".join(f"- **{label}:** {_pct(r.get(k))}" for k, label in order if r.get(k) is not None)


def build_fund_brief(con: duckdb.DuckDBPyConnection, scheme_code: int, *,
                     backfill: bool = True) -> str | None:
    """Markdown fund report for ``scheme_code``. Backfills history first (best-effort)
    so returns/risk are meaningful on a first-ever request. None if the scheme is unknown."""
    if backfill:
        s = funds.nav_series(con, scheme_code)
        if s.empty or (s.index[-1] - s.index[0]).days < 400:   # thin → pull the AMC's history
            try:
                backfill_mf_scheme_history(con, scheme_code)
            except Exception:  # noqa: BLE001 — report on whatever history we have
                pass
    d = funds.summary(con, scheme_code)
    if d is None:
        return None
    risk, roll = d["risk"], d["rolling_1y"]
    pct = funds.category_percentile(con, scheme_code, "3y") \
        or funds.category_percentile(con, scheme_code, "1y")

    lines = [
        f"# {d['scheme_name']}",
        f"*{d['amc']} · {d['category']} · {d['plan']}/{d['option']}*",
        "",
        f"**NAV ₹{d['nav']:,.2f}** (as of {d['nav_date']:%d-%b-%Y}) · "
        f"{d['history_days'] / 365.25:.1f}y of NAV history on file",
        "",
        "## 📈 Returns",
        _returns_block(d["returns"]) or "_Insufficient history for trailing returns._",
    ]
    if pct:
        lines += ["", f"↳ **{pct['horizon']} return ranks #{pct['rank']} of {pct['n']}** in "
                  f"*{d['category']}* (top {pct['percentile']}% · category median "
                  f"{_pct(pct['category_median'])})."]
    lines += [
        "",
        "## ⚖️ Risk (trailing, from daily NAV)",
        f"- **Annualised volatility:** {_pct(risk['vol_pct'], plus=False)}",
        f"- **Sharpe:** {risk['sharpe'] if risk['sharpe'] is not None else 'n/a'} · "
        f"**Sortino:** {risk['sortino'] if risk['sortino'] is not None else 'n/a'}",
        f"- **Max drawdown:** {_pct(risk['max_drawdown_pct'])}",
    ]
    if any(v is not None for v in roll.values()):
        lines += ["", "## 🔁 Rolling 1-year returns (consistency)",
                  f"- **Worst / median / best:** {_pct(roll['min'])} · {_pct(roll['median'])} · {_pct(roll['max'])}"]

    # ---- SIP / XIRR: what a monthly investor actually earned ----
    series = funds.nav_series(con, scheme_code)
    sip = funds.sip_returns(series)
    if sip:
        lines += ["", "## 💰 If you had SIP'd ₹10,000/month",
                  "| Horizon | Installments | Invested | Value today | Gain | **XIRR** |",
                  "|---|---|---|---|---|---|"]
        for label in ("1y", "3y", "5y"):
            s = sip.get(label)
            if s:
                lines.append(f"| {label} | {s['installments']} | ₹{s['invested']:,} | "
                             f"₹{s['value']:,} | {_pct(s['gain_pct'])} | "
                             f"**{_pct(s['xirr_pct'])}** |")
        lines.append("_XIRR is the money-weighted return — what an SIP actually earns, which "
                     "differs from the lump-sum CAGR above because each instalment is invested "
                     "for a different length of time._")

    # ---- benchmark-relative behaviour ----
    bench_name = funds.benchmark_for(d.get("category"), d.get("asset_class"))
    bm = None
    if bench_name:
        bm = funds.benchmark_metrics(series, funds.index_series(con, bench_name))
    if bm:
        beat = bm["fund_ann_pct"] - bm["bench_ann_pct"]
        lines += ["", f"## 🎯 Versus its benchmark ({bench_name})",
                  f"- **Fund {_pct(bm['fund_ann_pct'])} vs benchmark {_pct(bm['bench_ann_pct'])} "
                  f"annualised** → {'outperformed' if beat >= 0 else 'lagged'} by "
                  f"**{abs(beat):.1f} pp** over the last {bm['years']}y of common history.",
                  f"- **Alpha {_pct(bm['alpha_pct'])}** (risk-adjusted excess return) · "
                  f"**Beta {bm['beta']}** ({'more' if bm['beta'] > 1 else 'less'} volatile than "
                  f"the index)",
                  f"- **Up-capture {bm['up_capture_pct']}%** / **down-capture "
                  f"{bm['down_capture_pct']}%** — it captures {bm['up_capture_pct']}% of the "
                  f"index's up-moves and {bm['down_capture_pct']}% of its falls "
                  f"({'good' if (bm['down_capture_pct'] or 100) < 100 else 'poor'} downside "
                  "protection).",
                  f"- **Tracking error {_pct(bm['tracking_error_pct'], plus=False)}** · "
                  f"**Information ratio {bm['information_ratio']}** (excess return per unit of "
                  "active risk).",
                  f"_Based on {bm['n_days']} overlapping trading days (~{bm['years']:.0f}y of "
                  "daily NAV vs the index)._"]
    elif bench_name:
        lines += ["", f"## 🎯 Versus its benchmark ({bench_name})",
                  "_Not enough overlapping index history yet for alpha/beta (needs ~60 common "
                  "trading days)._"]

    snap = funds.holdings_snapshot(con, scheme_code)
    if snap:
        lines += ["", f"## 🧬 Portfolio (as of {snap['as_of']:%b-%Y})",
                  f"- **{snap['n_holdings']} holdings** · top-10 = **{snap['top10_pct']}%** of NAV"
                  + (f" · biggest sector **{snap['top_sector']}** ({snap['top_sector_pct']}%)"
                     if snap['top_sector'] else ""),
                  "- **Top holdings:** " + ", ".join(f"{n} ({p}%)" for n, p in snap["top"][:8])]
        ov = funds.watchlist_overlap(con, scheme_code)
        if ov and ov["hits"]:
            names = ", ".join(f"{s} ({p}%)" for s, p in ov["hits"][:12])
            lines += [f"- **Overlap with your watchlist:** holds **{len(ov['hits'])} of "
                      f"{ov['n_watchlist']}** names = **{ov['weight_pct']}%** of NAV — {names}"]

        # ---- what the manager actually did last month ----
        ch = funds.holdings_churn(con, scheme_code)
        if ch and (ch["new"] or ch["exited"] or ch["added"] or ch["trimmed"]):
            def _row(items, sign=False):
                return ", ".join(f"{n} ({v:+.2f}pp)" if sign else f"{n} ({v}%)"
                                 for n, v in items)
            lines += ["", f"## 🔄 What the manager did ({ch['previous']:%b} → {ch['current']:%b})"]
            if ch["new"]:
                lines.append(f"- 🟢 **Bought {ch['n_new']} new:** {_row(ch['new'])}")
            if ch["exited"]:
                lines.append(f"- 🔴 **Exited {ch['n_exited']}:** {_row(ch['exited'])}")
            if ch["added"]:
                lines.append(f"- ➕ **Added to:** {_row(ch['added'], sign=True)}")
            if ch["trimmed"]:
                lines.append(f"- ➖ **Trimmed:** {_row(ch['trimmed'], sign=True)}")
            lines.append("_Straight from consecutive SEBI-mandated monthly portfolio "
                         "disclosures — the manager's actual conviction, not commentary._")

    lines += ["", "---", "_NAV/returns from AMFI · holdings from the AMC's SEBI monthly "
              "disclosure (primary). Deeper forensic look-through (Altman/Piotroski across "
              "holdings) grows with financials coverage._"]
    return "\n".join(lines)
