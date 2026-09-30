"""📋 Corporate-Action Desk report — what's happening to the stocks you hold, and whether you need to act."""

from __future__ import annotations

from datetime import date

from equity_research.analysis.ca_desk import Action

DISCLAIMER = ("_Dates and figures come from NSE's corporate-action record and the company's own filings (cited). "
              "This lays out what happens and what each choice is worth — it isn't advice to tender, apply or "
              "sell, and it doesn't cover tax: check the offer letter and your broker's instructions._")
_TITLE = {"buyback": "🔁 Buyback", "rights": "🎟️ Rights issue", "demerger": "✂️ Demerger", "bonus": "🎁 Bonus",
          "split": "🔪 Split", "dividend": "💰 Dividend"}


def _when(d: date | None, today: date) -> str:
    if not d:
        return "—"
    n = (d - today).days
    rel = "today" if n == 0 else (f"in {n} day{'s' * (n != 1)}" if n > 0 else f"{-n} day{'s' * (n != -1)} ago")
    return f"{d:%d-%b-%Y} ({rel})"


def _cite(a: Action, field: str) -> str:
    fid = (a.details.get("cite") or {}).get(field)
    url = (a.evidence.get(fid) or {}).get("url") if fid else None
    return f" ([{fid}]({url}))" if url else (f" ({fid})" if fid else "")


def _row(a: Action, label: str, field: str, fmt=str) -> str | None:
    v = a.details.get(field)
    return f"- **{label}:** {fmt(v)}{_cite(a, field)}" if v not in (None, "") else None


def _rs(v) -> str:
    return f"₹{float(v):,.2f}"


def _body(a: Action, today: date) -> list[str]:
    d, px = a.details, a.price
    lines: list[str] = []
    if a.closed_on:
        lines.append(f"**Nothing left to do** — the {'tender' if a.kind == 'buyback' else 'application'} window "
                     f"closed {_when(a.closed_on, today)}.")
    if a.closed_on and a.kind in ("buyback", "rights"):
        lines += [_row(a, "Offer price" if a.kind == "buyback" else "Issue price",
                       "offer_price" if a.kind == "buyback" else "issue_price", _rs)]
    elif a.kind == "buyback":
        lines.append("**You need to decide:** tender some or all of your shares into the buyback, or ignore it. "
                     "Ignoring it costs nothing — you simply keep your shares.")
        lines += [_row(a, "Method", "method"), _row(a, "Offer price", "offer_price", _rs)]
        if d.get("premium_pct") is not None and px:
            lines.append(f"- **vs today's price ({_rs(px)}):** {d['premium_pct']:+.1f}%")
        lines += [_row(a, "Size", "size_cr", lambda v: f"₹{float(v):,.0f} cr"),
                  _row(a, "Tender window opens", "open_date"), _row(a, "Tender window closes", "close_date"),
                  _row(a, "Small-shareholder entitlement", "small_shareholder_entitlement"),
                  _row(a, "General entitlement", "general_entitlement")]
        lines.append("_Only shares held on the record date count. Usually fewer shares are accepted than tendered "
                     "(the entitlement ratio); the rest come back to you. Tendering happens through your broker._")
    elif a.kind == "rights":
        lines.append("**You need to decide:** apply (pay for new shares), sell your rights entitlements (RE) in "
                     "the market, or do nothing. **Doing nothing is the only choice that loses money** — the "
                     "entitlements lapse and your stake is diluted.")
        if d.get("ratio"):
            lines.append(f"- **Ratio:** {d['ratio']}")
        lines.append(_row(a, "Issue price", "issue_price", _rs))
        if d.get("value_per_right") is not None:
            lines.append(f"- **Each right is worth about:** {_rs(d['value_per_right'])} (today's {_rs(px)} − the "
                         "issue price)" if d["value_per_right"] > 0 else
                         f"- **Issue price is above today's price ({_rs(px)})** — the rights are worth little or "
                         "nothing right now; applying means paying more than the market price.")
        if d.get("dilution_if_ignored_pct") is not None:
            lines.append(f"- **If you ignore it:** the theoretical ex-rights price is {_rs(d['terp'])}, about "
                         f"{d['dilution_if_ignored_pct']:.1f}% below today — that's what your holding loses")
        lines += [_row(a, "Payment", "payment_terms"), _row(a, "Issue opens", "open_date"),
                  _row(a, "Last day to sell / renounce the rights", "renunciation_last_date"),
                  _row(a, "Issue closes (last day to apply)", "close_date")]
        lines.append("_The RE shares appear in your demat after the record date; apply via your broker / ASBA._")
    elif a.kind == "demerger":
        lines.append("**Nothing to do** — the new company's shares arrive in your demat automatically. But your "
                     "**cost gets split**: the parent will trade lower after the ex-date. That drop is not a loss; "
                     "part of your value (and your cost) moves to the new shares.")
        lines += [_row(a, "New company", "new_company"), _row(a, "Ratio", "ratio"),
                  _row(a, "New shares list", "listing_date"), _row(a, "Cost split (from the company)", "cost_split")]
        if not d.get("cost_split"):
            lines.append("- **Cost split:** not filed yet — companies usually announce the cost-of-acquisition "
                         "split after the new shares list; ask again then.")
        lines.append("_Until the new shares list, your broker app may show the parent at a fake loss._")
    elif a.kind in ("bonus", "split"):
        meaning = d.get("meaning", "your share count changes and the price adjusts")
        lines.append(f"**Nothing to do.** {meaning[0].upper()}{meaning[1:]} — your holding's value doesn't change. "
                     "Your broker app may briefly show a fake drop until the new shares arrive.")
    elif a.kind == "dividend":
        amt = d.get("per_share")
        lines.append("**Nothing to do** — it's paid to your bank account if you hold the shares before the ex-date."
                     + (f" ₹{amt:g} per share" + (f" (~{d['yield_pct']:.2f}% of today's price)" if d.get("yield_pct")
                                                   else "") + "." if amt else ""))
    if d.get("note"):
        lines.append(f"- _{d['note']}_")
    return [x for x in lines if x]


def render(actions: list[Action], *, today: date | None = None, n_holdings: int = 0) -> str:
    today = today or date.today()
    head = "# 📋 Corporate-Action Desk — your holdings"
    if not actions:
        return (f"{head}\n\nNothing coming up or just done across your {n_holdings} holding(s) — no buybacks, "
                "rights issues, demergers, bonuses, splits or dividends in the last 45 or next 90 days.\n\n"
                "_It covers the watchlist's **holdings** and any stock with a `thesis:`._\n\n---\n" + DISCLAIMER)
    act = [a for a in actions if a.needs_action]
    info = [a for a in actions if not a.needs_action]
    parts = [head, f"**{len(act)} need{'s' * (len(act) == 1)} a decision from you · {len(info)} need nothing**"
             if act else f"**Nothing needs a decision from you** — {len(info)} action(s), all automatic."]
    for title, group in (("⚠️ Needs your decision", act), ("✅ Nothing to do — just so you know", info)):
        if not group:
            continue
        parts.append(f"## {title}")
        for a in group:
            meta = f"Ex-date {_when(a.ex_date, today)}"
            if a.record_date and a.record_date != a.ex_date:
                meta += f" · record date {a.record_date:%d-%b-%Y}"
            parts.append(f"### {_TITLE.get(a.kind, a.kind)} — {a.name} ({a.symbol})\n\n_{a.subject}_ · {meta}\n\n"
                         + "\n".join(_body(a, today)))
    parts.append("---\n" + DISCLAIMER)
    return "\n\n".join(parts)


def render_new(actions: list[Action], *, today: date | None = None) -> str:
    """The push: only actions not seen before."""
    return render(actions, today=today).replace("# 📋 Corporate-Action Desk — your holdings",
                                                "# 📋 Corporate-Action Desk — new on your holdings", 1)
