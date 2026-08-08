"""Proactive, trigger-based screen digest — one weekly email of what *changed*.

The holdco / fundamental / investor screens are pull-only (you email `screen: …`). This
turns them into a **push**: once a week, run all three, compare against the last run's
fingerprint (persisted in ``alert_state``), and email **only the deltas** — a holdco whose
discount widened or newly appeared, a stock entering / climbing the fundamental ranks, and
a marquee investor's fresh moves. If nothing crossed a threshold, no email is sent.

State lives in ``alert_state`` ``__meta__`` rows (same store the daily scan uses). The
fingerprint is advanced only *after* the digest is delivered (``commit_screen_state``), so a
crash before delivery re-surfaces the same deltas next time rather than silently eating them.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import holdco, investors, screener
from equity_research.common.db import connect
from equity_research.reports import md

_IST = ZoneInfo("Asia/Kolkata")

# --- trigger thresholds (tune here) ---
HOLDCO_WIDEN = 5.0        # pp — report a holdco whose discount deepened by at least this
FUND_TOP_N = 15           # report a stock entering the top-N of the fundamental screen
FUND_JUMP = 10            # …or climbing at least this many rank places
# investor moves already use investors.DETECT_FLOOR (0.5) for what counts as a move
_INVESTOR_PRUNE_DAYS = 220   # forget reported investor-move keys older than ~2 quarters


# ----------------- meta state (alert_state __meta__) -----------------
def _meta(con, key):
    r = con.execute("SELECT value FROM alert_state WHERE symbol='__meta__' AND key=?",
                    [key]).fetchone()
    return r[0] if r else None


def _set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                "VALUES ('__meta__', ?, ?, now())", [key, value])


# ----------------- delta computation -----------------
def _holdco_delta(con) -> tuple[list[dict], dict]:
    """(triggered rows, new fingerprint). A row triggers if it's newly discounted or its
    discount widened ≥ HOLDCO_WIDEN pp vs last run."""
    try:
        rows = holdco.holdco_discounts(con, limit=30)
    except Exception:  # noqa: BLE001
        return [], json.loads(_meta(con, "screen_fp_holdco") or "{}")
    last = json.loads(_meta(con, "screen_fp_holdco") or "{}")
    out, fp = [], {}
    for r in rows:
        disc = r["discount_pct"]
        if disc is None:
            continue
        fp[r["holder"]] = round(disc, 1)
        prev = last.get(r["holder"])
        if prev is None:
            out.append({**r, "kind": "new", "prev": None})
        elif disc - prev >= HOLDCO_WIDEN:
            out.append({**r, "kind": "widened", "prev": prev})
    out.sort(key=lambda r: -(r["discount_pct"] or 0))
    return out, fp


def _fundamental_delta(con) -> tuple[list[dict], dict]:
    """(triggered rows, new fingerprint). A stock triggers if it newly enters the top-N or
    climbs ≥ FUND_JUMP places."""
    try:
        rows = screener.fundamental_screen(con, limit=30)
    except Exception:  # noqa: BLE001
        return [], json.loads(_meta(con, "screen_fp_fundamental") or "{}")
    last = json.loads(_meta(con, "screen_fp_fundamental") or "{}")
    out, fp = [], {}
    for rank, r in enumerate(rows, 1):
        fp[r["symbol"]] = rank
        prev = last.get(r["symbol"])
        if rank <= FUND_TOP_N and (prev is None or prev > FUND_TOP_N):
            out.append({**r, "rank": rank, "prev_rank": prev, "kind": "entrant"})
        elif prev is not None and prev - rank >= FUND_JUMP:
            out.append({**r, "rank": rank, "prev_rank": prev, "kind": "climbed"})
    out.sort(key=lambda r: r["rank"])
    return out, fp


def _investor_delta(con) -> tuple[list[dict], list[str]]:
    """(new move rows, updated reported-key list). Reports each investor move not seen before."""
    try:
        moves = investors.all_moves(con)
    except Exception:  # noqa: BLE001
        return [], json.loads(_meta(con, "screen_fp_investors") or "[]")
    reported = set(json.loads(_meta(con, "screen_fp_investors") or "[]"))
    out, fresh_keys = [], set()
    for canon in investors.roster():
        m = moves.get(canon)
        if not m:
            continue
        for kind in ("entered", "added", "trimmed", "exited"):
            for r in m[kind]:
                key = f"{canon}|{r['symbol']}|{kind}|{r['as_of'].isoformat()}"
                fresh_keys.add(key)
                if key not in reported:
                    out.append({**r, "investor": canon, "kind": kind})
    # keep recent reported keys + this run's, prune stale ones so the list stays bounded
    cutoff = date.today().toordinal() - _INVESTOR_PRUNE_DAYS

    def _recent(k: str) -> bool:
        try:
            return date.fromisoformat(k.rsplit("|", 1)[1]).toordinal() >= cutoff
        except (ValueError, IndexError):
            return False
    updated = sorted({k for k in reported if _recent(k)} | fresh_keys)
    return out, updated


def build_screen_delta(con: duckdb.DuckDBPyConnection) -> dict:
    """Compute all three deltas without persisting. Returns
    ``{holdco, fundamental, investors, _fp}`` where ``_fp`` carries the fingerprints to
    persist via ``commit_screen_state`` only after the email is delivered."""
    hc, hc_fp = _holdco_delta(con)
    fu, fu_fp = _fundamental_delta(con)
    inv, inv_keys = _investor_delta(con)
    return {"holdco": hc, "fundamental": fu, "investors": inv,
            "_fp": {"holdco": hc_fp, "fundamental": fu_fp, "investors": inv_keys}}


def commit_screen_state(con: duckdb.DuckDBPyConnection, delta: dict) -> None:
    """Persist the fingerprints — call ONLY after the digest email is sent."""
    fp = delta.get("_fp", {})
    _set_meta(con, "screen_fp_holdco", json.dumps(fp.get("holdco", {})))
    _set_meta(con, "screen_fp_fundamental", json.dumps(fp.get("fundamental", {})))
    _set_meta(con, "screen_fp_investors", json.dumps(fp.get("investors", [])))
    _set_meta(con, "last_screen_week", _iso_week(datetime.now(_IST)))


# ----------------- formatting -----------------
def _crore(v) -> str:
    if v is None:
        return "n/a"
    return f"₹{v/1e5:,.2f}L cr" if v >= 1e5 else f"₹{v:,.0f} cr"


def _holdco_reading(r: dict) -> str:
    """Plain-English, row-specific gloss on the discount sign."""
    d = r["discount_pct"]
    if d >= 0:
        return f"trades ~{d:.0f}% **below** its listed stakes"
    return f"trades ~{abs(d):.0f}% **above** its listed stakes"


def format_screen_digest(delta: dict) -> str | None:
    """One markdown email covering the three screens' deltas. None if nothing triggered."""
    hc, fu, inv = delta["holdco"], delta["fundamental"], delta["investors"]
    if not (hc or fu or inv):
        return None
    today = datetime.now(_IST).date()
    parts = [
        f"# 📡 Screener movements — week of {today:%d-%b-%Y}",
        "_Only what **changed** since the last digest — each name shows up **once**, when it "
        "first moves, not every week. Reply `screen: holdco`, `screen: value` or "
        "`screen: investors` for the full live lists._",
        "**⏱️ What 'changed' is measured against:** Holdco discounts and the fundamental screen "
        "are compared **week-over-week** (this Saturday's run vs the previous digest). "
        "Marquee-investor moves come from **quarterly** SEBI shareholding filings, so they compare "
        "the latest filed quarter with the one before and only refresh when a new quarter is "
        "filed (~every 3 months) — not weekly.",
    ]

    if hc:
        rows = [[("🆕 new" if r["kind"] == "new" else "📈 wider"),
                 r["holder"], (r.get("holder_name") or r["holder"])[:24],
                 f"{r['discount_pct']:+.0f}%",
                 ("—" if r["prev"] is None else f"{r['prev']:+.0f}%"),
                 _crore(r["stake_nav_cr"]), _holdco_reading(r)] for r in hc]
        parts += [
            "## 🏦 Holdco discounts",
            md.table(["Change", "Holdco", "Company", "Discount", "Was", "Stake NAV",
                      "What it means"], rows, "lllrrrl"),
            "_**Discount** = how far the holding company's own market value sits **below** the "
            "market value of the listed stakes it owns. **Positive %** = it trades *below* those "
            "stakes — a genuine **holdco discount** (potentially cheap, the Elcid situation). "
            "**Negative %** = it trades *above* them — a **premium**, usually because big "
            "**unlisted / operating** businesses aren't counted here (only disclosed listed stakes "
            "are valued). **Was** = the reading in the previous digest; **'—' = newly surfaced** "
            "(no prior reading). **🆕 new** = first time it cleared the screen · **📈 wider** = "
            "discount deepened ≥5pp since last week._"]

    if fu:
        rows = [[("🆕 entered top-15" if r["kind"] == "entrant"
                  else (f"📈 up from #{r['prev_rank']}" if r.get("prev_rank") else "📈 climbed")),
                 r["rank"], r["symbol"], r["name"][:24],
                 f"{r['composite']:.1f}", r.get("why", "")] for r in fu]
        parts += [
            "## 🔎 Fundamental screen (Nifty-500)",
            md.table(["Change", "Rank", "Symbol", "Company", "Score", "Why (breakdown)"],
                     rows, "lrllrl"),
            "_**Rank** = position in the **full Nifty-500 ranking**; only names that **changed** "
            "appear, so the numbers skip (a rank you don't see just means that name held its "
            "place). **Score** (0–100) is a weighted blend — **40% Quality** (Piotroski F, 0–9) + "
            "**35% Forensic** (Altman-Z solvency, Beneish-M earnings quality, low accruals, no "
            "promoter pledge; 0–4) + **25% Cheapness** (how low today's P/E — P/B for financials — "
            "sits vs the stock's own history). Higher = stronger on all three; the last column is "
            "the raw per-pillar breakdown behind the score._"]

    if inv:
        arrow = {"entered": "🟢 new", "added": "➕ add", "trimmed": "➖ trim", "exited": "🔴 exit"}
        rows = []
        for r in inv:
            if r["kind"] == "entered":
                change = f"new → {r['pct']:.2f}%"
            elif r["kind"] == "exited":
                change = f"{r['prev_pct']:.2f}% → out"
            else:
                change = f"{r['prev_pct']:.2f}% → {r['pct']:.2f}%"
            rows.append([r["investor"], arrow[r["kind"]], r["symbol"],
                         r["name"][:22], change])
        parts += [
            "## 👤 Marquee-investor moves",
            md.table(["Investor", "Move", "Symbol", "Company", "Stake (prev → now)"],
                     rows, "lllll"),
            "_**Stake** = the investor's **% of the company's shares**, previous filed quarter → "
            "latest (so **+0.8** points of stake reads as e.g. `1.2% → 2.0%`). Only holders "
            "**disclosed by name** (≥~1%) are visible, so an **exit** can be a full sale *or* a "
            "trim below the ~1% disclosure floor. From quarterly SEBI filings; coverage grows with "
            "shareholding data ingested._"]

    return "\n\n".join(parts)


# ----------------- cadence -----------------
def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def due_this_week(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    """True once per ISO week — hasn't run yet in the current week."""
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_screen_week") != _iso_week(datetime.now(_IST))
    finally:
        if own:
            con.close()
