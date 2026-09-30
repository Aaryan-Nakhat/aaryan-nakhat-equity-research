"""🔍 Reality Check report — renders ``analysis.reality_check.Result`` as markdown."""

from __future__ import annotations

from equity_research.analysis.reality_check import BENCHMARK, Result

_STATUS = {"confirmed": "✅ Confirmed", "partly": "🟡 Partly true", "contradicted": "❌ Contradicted",
           "not_found": "⚠️ No filing found", "unverifiable": "❓ Can't be verified"}
_KIND = {"x": "X post", "reddit": "Reddit post", "article": "Article", "text": "Pasted text"}

DISCLAIMER = ("_Not investment advice. This checks what a post claims against exchange filings and "
              "reported numbers; a confirmed claim can still be a bad trade, and the stocks named here "
              "are the post's, not a recommendation._")


def _pct(v, signed=True) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def build(res: Result) -> dict:
    """``{markdown, picks}`` — picks are the resolved companies, for a numbered deep-report menu."""
    p = res.post
    src = _KIND.get(p.kind, "Post")
    meta = " · ".join(x for x in (p.title, p.author and f"by {p.author}", p.published) if x)
    head = ["# 🔍 Reality Check",
            f"> **{src}**" + (f" — {meta}" if meta else "") + (f" · [link]({p.url})" if p.url else "")]
    if res.extracted:
        head.append(f"> _{res.extracted.get('summary', '')}_")
    label, why = res.verdict
    parts = ["\n".join(head), f"## Bottom line: {label}\n\n{why}"]

    rows = []
    for i, c in enumerate(res.claims, 1):
        who = ", ".join(c["symbols"]) or ", ".join(x.get("name", "") for x in c.get("companies") or []) or "—"
        status = _STATUS.get(c.get("status"), "❓")
        if c.get("evidence_url"):
            status += f" ([filing]({c['evidence_url']}))"
        note = c.get("note") or ""
        rows.append(f"| {i} | {c['text']} | {who} | {status} | {note} |")
    if rows:
        parts.append("## What the post claims — and what the record says\n\n"
                     "| # | Claim | Company | Check | Detail |\n|---|---|---|---|---|\n" + "\n".join(rows))
        if not res.filings_available:
            parts.append("_Exchange filings weren't checked — NSE access is off (`NSE_SCRAPING_ENABLED`), "
                         "so claims were checked against reported numbers only._")

    sized = [c for c in res.claims if c.get("pct_rev") is not None or c.get("pct_mcap") is not None]
    if sized:
        lines = ["| Claim | Amount | vs annual revenue | vs market cap |", "|---|---|---|---|"]
        for c in sized:
            lines.append(f"| {c['text'][:60]} | ₹{c['amount_cr']:,.0f} cr | {_pct(c.get('pct_rev'), False)} | "
                         f"{_pct(c.get('pct_mcap'), False)} |")
        parts.append("## How big is it?\n\n" + "\n".join(lines) +
                     "\n\n_Under ~5% of annual revenue (or ~2% of market cap) rarely moves the needle._")

    if res.companies:
        lines = [f"| Company | Market cap | 5 sessions vs {BENCHMARK} | 20 sessions | Since the post | Volume vs usual |",
                 "|---|---|---|---|---|---|"]
        for co in res.companies.values():
            m = co.moves
            since = _pct(m.get("since")) + (f" (from {m['since_date']:%d-%b})" if m.get("since_date") else "")
            lines.append(f"| {co.name} ({co.symbol}) | "
                         f"{'₹' + format(co.mcap_cr, ',.0f') + ' cr' if co.mcap_cr else '—'} | {_pct(m.get('ex5'))} | "
                         f"{_pct(m.get('ex20'))} | {since if m.get('since') is not None else '—'} | "
                         f"{str(round(m['vol_x'], 1)) + '×' if m.get('vol_x') else '—'} |")
        parts.append("## Is it already in the price?\n\n" + "\n".join(lines) +
                     "\n\n_Moves are the stock's return minus the market's, on split-adjusted prices._")

    flag_lines = [f"- **{co.symbol}:** {f}" for co in res.companies.values() for f in co.flags]
    if res.hype:
        flag_lines.append("- **Promotional language:** " + ", ".join(f"“{h}”" for h in res.hype[:8]))
    parts.append("## 🚩 Red flags\n\n" + ("\n".join(flag_lines) if flag_lines else "None found."))

    if res.also_affected:
        parts.append("## Also affected (AI-suggested — verify)\n\n" + "\n".join(
            f"- **{a['name']} ({a['symbol']})** — {a['why']}" for a in res.also_affected))
    if res.unresolved:
        parts.append("_Couldn't match to an NSE listing (or the name fits several): "
                     + ", ".join(res.unresolved) + "._")

    picks = [{"symbol": co.symbol, "name": co.name} for co in res.companies.values()] + \
            [{"symbol": a["symbol"], "name": a["name"]} for a in res.also_affected]
    if picks:
        parts.append("**Want the full picture?** Pick a number for that company's deep report:\n\n" + "\n".join(
            f"{i}. {x['name']} ({x['symbol']})" for i, x in enumerate(picks, 1)))
    parts.append("---\n" + DISCLAIMER)
    return {"markdown": "\n\n".join(parts), "picks": picks}
