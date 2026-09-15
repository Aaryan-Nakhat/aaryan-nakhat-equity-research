"""🎙️ Concalls — market-wide earnings-call discovery (the `concalls` command; this module is
named ``call_radar`` internally).

Surfaces the notable recent earnings calls across the market: it reads management's forward
**Management Tone from the words** (an LLM read of the transcript) and cross-checks it against the
quarter's actual **Execution from the numbers** (computed deterministically from our own financials).
The divergence — the **say-do gap** — is the edge: bullish talk on soft numbers is a caution flag;
guarded talk on strong numbers is a quietly-delivering name worth a look.

Scoring is LLM-heavy, so it runs **incrementally in the background** (only newly-filed transcripts,
bounded per run) and is **persisted** to the ``concall_signals`` table; the on-demand command and the
weekly push just read + rank that table. Output is a ranked list → reply a number → deep report.
An idea generator, not a call.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import date, datetime, timedelta

import duckdb

from equity_research.analysis import alerts, fundamentals
from equity_research.common.http import fetch_bytes
from equity_research.reports import synthesize
from equity_research.scrapers import nse_api

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 45          # how far back the market-wide announcement sweep looks
_RADAR_WINDOW_DAYS = 35      # how recent a scored call must be to appear on the radar

# Management Tone (words) / Execution (numbers) → a 0-4 ordinal, so the say-do gap and the signal
# score are computable. Higher = more confident / stronger.
_TONE_VAL = {"Very Confident": 4, "Confident": 3, "Balanced": 2, "Cautious": 1, "Defensive": 0}
_EXEC_VAL = {"Firing": 4, "Delivering": 3, "Holding": 2, "Slipping": 1, "Struggling": 0}
_GAP_THRESHOLD = 2          # tone-vs-execution ordinal gap that counts as a real divergence


def _universe(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Symbols with financials ingested — the delivery cross-check is only real for these."""
    return {r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials").fetchall()}


def _dt(a: dict) -> datetime:
    try:
        return datetime.strptime((a.get("an_dt") or "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def _is_transcript(desc: str, text: str) -> bool:
    """True when the filing is an actual earnings-call **transcript** (carries the forward words +
    Q&A) — not just any investor-meet intimation."""
    blob = f"{desc} {text}".lower()
    title, _, _ = alerts._categorise(desc, text, False)
    return title == "Concall / investor meet" and "transcript" in blob


def pending_transcripts(con: duckdb.DuckDBPyConnection, *, days: int = _LOOKBACK_DAYS,
                        universe: set[str] | None = None) -> list[dict]:
    """New transcript filings (market-wide, last ``days``) not yet scored — newest first. One cheap
    NSE call; filtered to transcripts in the ``universe`` (symbols with financials) and deduped
    against ``concall_signals``. Returns ``[{symbol, filed_date, url, desc}, …]``. Never raises."""
    uni = universe if universe is not None else _universe(con)
    f = (date.today() - timedelta(days=days)).strftime("%d-%m-%Y")
    t = date.today().strftime("%d-%m-%Y")
    try:
        raw = nse_api.corporate_announcements(from_date=f, to_date=t)
    except Exception:  # noqa: BLE001 — sweep is best-effort
        log.exception("call-radar: market-wide announcement sweep failed")
        return []
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    done = {(s, d) for s, d in con.execute(
        "SELECT symbol, filed_date FROM concall_signals").fetchall()}
    seen: set[tuple[str, date]] = set()
    out: list[dict] = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        sym = (a.get("symbol") or "").strip().upper()
        att = (a.get("attchmntFile") or "").strip()
        if not sym or sym not in uni or not att.lower().endswith(".pdf"):
            continue
        if not _is_transcript(a.get("desc", ""), a.get("attchmntText", "")):
            continue
        fdate = _dt(a).date()
        if fdate == date.min or (sym, fdate) in done or (sym, fdate) in seen:
            continue
        seen.add((sym, fdate))
        out.append({"symbol": sym, "filed_date": fdate, "url": att,
                    "desc": f"{a.get('desc', '')} {a.get('attchmntText', '')}".strip()})
    out.sort(key=lambda r: r["filed_date"], reverse=True)
    return out


def _pdf_has_pages(data: bytes) -> bool:
    """True if ``data`` parses as a PDF with ≥1 page (drops broken/empty files before the LLM). If
    no PDF parser is installed, assume OK (the LLM layer degrades on a bad doc anyway)."""
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return True
    try:
        return len(PdfReader(io.BytesIO(data)).pages) > 0
    except Exception:  # noqa: BLE001
        return False


def _gap(tone: str, execution: str | None) -> str:
    """The say-do gap: the forward Management Tone vs the delivered Execution (both 0-4 ordinals)."""
    if execution is None:
        return "Numbers n/a"
    diff = _TONE_VAL[tone] - _EXEC_VAL[execution]
    if diff >= _GAP_THRESHOLD:
        return "Talk > Numbers"        # more upbeat than the numbers justify — caution
    if diff <= -_GAP_THRESHOLD:
        return "Numbers > Talk"        # delivering more than management is talking up — under-radar
    return "Aligned"


def _signal_score(tone: str, execution: str | None) -> float:
    """0-100 notability: the say-do **divergence** carries the most weight (the edge), with a bonus
    for overall strength so a clean strong-and-confident call also surfaces."""
    t = _TONE_VAL[tone]
    e = _EXEC_VAL.get(execution, 2)             # unknown execution treated as neutral (Holding)
    divergence = abs(t - e)                       # 0..4
    strength = t + e                              # 0..8
    return float(min(100, 40 + divergence * 10 + strength * 2.5))


def score_one(con: duckdb.DuckDBPyConnection, symbol: str, url: str, filed_date: date, *,
              model: str | None = None) -> dict | None:
    """Fetch + score one transcript and upsert it into ``concall_signals``. Returns the stored row
    dict, or ``None`` if the PDF or the LLM read failed. Best-effort."""
    try:
        data = fetch_bytes(url)
    except Exception:  # noqa: BLE001
        log.warning("call-radar: fetch failed for %s (%s)", symbol, url)
        return None
    if not data or not _pdf_has_pages(data):
        log.warning("call-radar: unreadable transcript for %s — skipping", symbol)
        return None
    kwargs = {"model": model} if model else {}
    sig = synthesize.concall_signal([(f"{symbol} transcript", data)], **kwargs)
    if not sig:
        return None
    tone = sig["tone"]
    execution = fundamentals.execution_band(con, symbol)
    gap = _gap(tone, execution)
    score = _signal_score(tone, execution)
    takeaways = sig.get("takeaways") or []
    summary_md = "\n".join(f"- {t}" for t in takeaways if isinstance(t, str))
    guidance = sig.get("guidance")
    guidance_json = json.dumps(guidance) if isinstance(guidance, dict) else None
    row = {
        "symbol": symbol, "filed_date": filed_date, "quarter": sig.get("quarter"),
        "tone": tone, "execution": execution, "gap": gap, "signal_score": score,
        "summary_md": summary_md, "guidance_json": guidance_json, "source_url": url,
        "model": model or "",
    }
    con.execute(
        """INSERT OR REPLACE INTO concall_signals
           (symbol, filed_date, quarter, tone, execution, gap, signal_score,
            summary_md, guidance_json, source_url, model, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, now())""",
        [row["symbol"], row["filed_date"], row["quarter"], row["tone"], row["execution"],
         row["gap"], row["signal_score"], row["summary_md"], row["guidance_json"],
         row["source_url"], row["model"]])
    log.info("concalls: scored %s (%s · %s · %s, signal %.0f)",
             symbol, tone, execution or "n/a", gap, score)
    return row


def ingest_new(con: duckdb.DuckDBPyConnection, *, max_new: int = 8,
               model: str | None = None) -> int:
    """Score up to ``max_new`` newly-filed transcripts (bounds each run's cost; a results-season
    backlog drains over successive passes). Returns how many were scored. Never raises."""
    try:
        pending = pending_transcripts(con)
    except Exception:  # noqa: BLE001
        log.exception("call-radar: pending scan failed")
        return 0
    scored = 0
    for p in pending[:max_new]:
        try:
            if score_one(con, p["symbol"], p["url"], p["filed_date"], model=model):
                scored += 1
        except Exception:  # noqa: BLE001 — one bad transcript never breaks the pass
            log.exception("call-radar: scoring failed for %s", p["symbol"])
    return scored


def radar(con: duckdb.DuckDBPyConnection, *, limit: int = 25,
          days: int = _RADAR_WINDOW_DAYS) -> list[dict]:
    """The ranked radar: the most notable scored calls in the last ``days``, biggest signal first.
    Returns ``[{symbol, name, sector, filed_date, quarter, tone, execution, gap, signal_score,
    summary_md, watchlist}, …]``."""
    cutoff = (date.today() - timedelta(days=days))
    rows = con.execute(
        """SELECT symbol, filed_date, quarter, tone, execution, gap, signal_score, summary_md
           FROM concall_signals WHERE filed_date >= ?
           ORDER BY signal_score DESC, filed_date DESC LIMIT ?""",
        [cutoff, limit]).fetchall()
    if not rows:
        return []
    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())
    watch = {r[0] for r in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = []
    for (sym, fdate, quarter, tone, execution, gap, score, summary) in rows:
        out.append({
            "symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
            "filed_date": str(fdate)[:10], "quarter": quarter or "—",
            "tone": tone, "execution": execution or "n/a", "gap": gap,
            "signal_score": round(float(score)), "summary_md": summary or "",
            "watchlist": sym in watch,
        })
    return out
