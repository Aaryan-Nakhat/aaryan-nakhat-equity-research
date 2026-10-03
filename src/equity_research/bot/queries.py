"""Subject-line parsers — each turns an email subject (or a web / CLI command) into a request, or None."""

from __future__ import annotations

import re

from equity_research.reports.inbox import EmailRequest


def _ipo_query(subject: str) -> tuple[str, str] | None:
    """Parse an 'ipo: ...' subject → ('list','ongoing'|'upcoming') or ('name', <query>); bare 'ipo' / 'ipos'
    → the ongoing list. None if it isn't an IPO request."""
    if re.fullmatch(r"\s*(?:re:\s*)?ipos?\s*", subject or "", flags=re.I):
        return ("list", "ongoing")
    m = re.match(r"^\s*(?:re:\s*)?ipo\s*[:\-]\s*(.+)$", subject, flags=re.I)
    if not m:
        return None
    val = m.group(1).strip()
    low = val.lower()
    if low in ("ongoing", "live", "current", "open", "active"):
        return ("list", "ongoing")
    if low in ("upcoming", "forthcoming", "coming", "new"):
        return ("list", "upcoming")
    return ("name", val)


def _screen_query(subject: str) -> str | None:
    """Parse a screener request → one of 'holdco' | 'investors' | 'smallcap' | 'policy' |
    'technical' | 'volume' | 'beaters' | 'institutions' | 'margins' | 'deleverage' | 'quality' |
    'hotlist' | 'value', 'unknown' for a name that isn't a screen (the reply lists them), or None if not a
    screen request. Accepts 'screen: <name>' and bare 'screen' (→ value)."""
    m = re.match(r"^\s*(?:re:\s*)?screen\s*[:\-]?\s*(.*)$", subject, flags=re.I)
    if not m:
        return None
    val = m.group(1).strip().lower()
    if val in ("holdco", "holdcos", "holding", "discount", "discounts"):
        return "holdco"
    if val in ("investors", "investor", "hni", "hnis", "marquee", "bigbull", "big bull"):
        return "investors"
    if val in ("smallcap", "smallcaps", "small cap", "small-cap", "capex", "smallcap capex"):
        return "smallcap"
    if val in ("policy", "policies", "scheme", "schemes", "govt", "government", "gov",
               "policy radar", "scheme radar", "budget"):
        return "policy"
    if val in ("volume", "volume breakout", "volume breakouts", "breakout", "breakouts"):
        return "volume"
    if val in ("beaters", "market beaters", "relative strength", "rs", "strength",
               "outperformers", "outperformer"):
        return "beaters"
    if val in ("institutions", "institutional", "institutional buying", "smart money", "smartmoney",
               "promoter buying", "adding"):
        return "institutions"
    if val in ("hotlist", "hot list", "hot", "multisignal", "multi-signal", "confluence", "top"):
        return "hotlist"
    if val in ("margins", "margin", "margin momentum", "expanding margins"):
        return "margins"
    if val in ("deleverage", "deleveraging", "debt payers", "debt", "debt reduction"):
        return "deleverage"
    if val in ("quality", "compounders", "compounder", "quality compounders"):
        return "quality"
    if val in ("technical", "technicals", "ta", "setup", "setups", "chart", "charts", "buy", "buys"):
        return "technical"
    if val in ("", "value", "values", "cheap", "undervalued", "fundamental", "fundamentals", "valuation"):
        return "value"
    return "unknown"


def _sector_query(subject: str) -> str | None:
    """Parse a sector request → the raw sector text (e.g. 'defence', 'pharma', 'list'), or None if
    not a `sector:` command. 'sector: list' / 'sector: help' returns the literal 'list'."""
    m = re.match(r"^\s*(?:re:\s*)?sector\s*[:\-]\s*(.+)$", subject, flags=re.I)
    if not m:
        return None
    return m.group(1).strip()


def _suppliers_query(subject: str) -> str | None:
    """Parse a supply-chain request ('suppliers: BEL', 'supply chain: HAL', 'ancillaries: <co>')
    → the company text, or None if not a suppliers command."""
    m = re.match(r"^\s*(?:re:\s*)?(?:suppliers?|supply\s*chain|ancillaries|ancillary|vendors?)"
                 r"\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m else None


def _booking_query(subject: str) -> bool:
    """True for a portfolio profit-booking-risk request ('booking', 'booking risk', 'profit
    booking', 'sell risk', 'booking:')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:booking(?:\s*risk)?|profit[\s-]*booking|sell[\s-]*risk)"
                         r"\s*[:\-]?\s*$", subject, flags=re.I))


def _policy_query(subject: str) -> bool:
    """True for a bare policy-radar request ('policy:', 'schemes', 'policy radar', 'govt schemes')
    outside the `screen:` prefix."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:policy|policies|schemes?|policy radar|scheme radar|"
                         r"govt schemes?|government schemes?)\s*[:\-]?\s*$", subject, flags=re.I))


def _tailwind_query(subject: str) -> bool:
    """True for a global supply-shock / Tailwind request ('tailwind', 'tailwind:', 'catalysts',
    'global catalysts', 'supply shock'), optionally with a trailing '--latest' / 'fresh' flag."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:tailwind|tailwinds|global\s*catalyst(?:s)?|"
                         r"catalyst(?:s)?|supply\s*shock(?:s)?)\s*[:\-]?\s*"
                         r"(?:(?:--?\s*)?(?:latest|fresh|refresh|new|now))?\s*$", subject, flags=re.I))


def _pickaxe_query(subject: str) -> bool:
    """True for a demand-side / Pickaxe request ('pickaxe', 'demand', 'demand:', 'demand surge',
    'trends', 'buy trends'), optionally with a trailing '--latest' / 'fresh' flag."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:pickaxe|pickaxes|demand(?:\s*surge(?:s)?)?|"
                         r"buy\s*trends?|trends?)\s*[:\-]?\s*"
                         r"(?:(?:--?\s*)?(?:latest|fresh|refresh|new|now))?\s*$", subject, flags=re.I))


def _hotlist_query(subject: str) -> bool:
    """True for a bare multi-signal Hotlist request ('hotlist', 'hotlist:', 'hot list'), optionally
    with a trailing '--latest' / 'fresh' flag. (The `screen: hotlist` form is handled by
    `_screen_query`.)"""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:hotlist|hot\s*list)\s*[:\-]?\s*"
                         r"(?:(?:--?\s*)?(?:latest|fresh|refresh|new|now))?\s*$", subject, flags=re.I))


def _alert_query(subject: str) -> tuple[str, str] | None:
    """Parse a 🔔 filing-alert command → ``(action, keyword)``, or None if not an alert command.
    action ∈ {add, list, remove, clear}; keyword is '' for list/clear.
      `alert: <kw>` / `watch: <kw>` / `alert add: <kw>`  → add
      `alerts` / `alert` / `alert list`                  → list
      `unalert: <kw>` / `alert remove: <kw>` / `stop alert: <kw>` → remove
      `alert clear` / `clear alerts`                     → clear"""
    s = subject.strip()
    s = re.sub(r"^\s*re:\s*", "", s, flags=re.I).strip()
    if re.match(r"^(?:alert\s+clear|clear\s+alerts?)\s*$", s, flags=re.I):
        return ("clear", "")
    if re.match(r"^alerts?\s*$", s, flags=re.I) or re.match(r"^alert\s+list\s*$", s, flags=re.I):
        return ("list", "")
    m = re.match(r"^(?:unalert|alert\s+remove|remove\s+alert|stop\s+alert)\s*[:\-]\s*(.+)$", s, flags=re.I)
    if m:
        return ("remove", m.group(1).strip())
    m = re.match(r"^(?:alert\s+add|alert|watch)\s*[:\-]\s*(.+)$", s, flags=re.I)
    if m:
        return ("add", m.group(1).strip())
    return None


def _calls_query(subject: str) -> bool:
    """True for a 🎙️ Concalls request ('concalls', 'concall', 'calls', 'earnings calls')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:concalls?|calls?|earnings\s*calls?)"
                         r"\s*[:\-]?\s*$", subject, flags=re.I))


def _thesis_query(subject: str) -> tuple[str, str, str] | None:
    """🛡️ Thesis Guard → (action, company, text): ('list','',''), ('remove', co, ''), ('show', co, ''),
    or ('set', co, reasons). `thesis: BEL — reasons…` (the company ends at —, -, |, : or 'because')."""
    s = re.sub(r"^\s*re:\s*", "", subject or "", flags=re.I).strip()
    if re.fullmatch(r"(?:my\s+)?theses|thesis(?:\s+list)?", s, flags=re.I):
        return ("list", "", "")
    m = re.match(r"^(?:unthesis|thesis\s+remove|drop\s+thesis|stop\s+thesis)\s*[:\-]\s*(.+)$", s, flags=re.I)
    if m:
        return ("remove", m.group(1).strip(), "")
    m = re.match(r"^thesis\s*[:\-]\s*(.+)$", s, flags=re.I | re.S)
    if not m:
        return None
    rest = m.group(1).strip()
    parts = re.split(r"\s+[—–|]\s+|\s+-\s+|\s*:\s+|\s+because\s+", rest, maxsplit=1, flags=re.I)
    co, why = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    return ("set", co, why) if why else ("show", co, "")


def _reality_query(req: EmailRequest) -> str | None:
    """🔍 Reality Check → what to check (a link and/or text), or None. `reality check: <link>`,
    `reality_check: …`, `reality: …`, `verify: …`, `tip: …`; with nothing after the colon, the email's
    first body line is used (so a long link can go in the body)."""
    m = re.match(r"^\s*(?:re:\s*)?(?:reality[\s_-]*check|reality|verify|tip)\s*[:\-]?\s*(.*)$",
                 req.subject or "", flags=re.I | re.S)
    if not m:
        return None
    arg = m.group(1).strip()
    if not arg and not re.match(r"^\s*(?:re:\s*)?(?:reality[\s_-]*check|verify)\s*[:\-]?\s*$",
                                req.subject or "", flags=re.I):
        return None                                         # a bare 'tip' / 'reality' isn't a request
    return arg or (req.body or "").strip() or None


def _scorecard_query(subject: str) -> bool:
    """True for a 📊 track-record request ('scorecard', 'track record', 'track', 'hit rate')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:score\s*card|track(?:\s*record)?|hit\s*rates?|"
                         r"how\s+did\s+we\s+do)\s*[:\-]?\s*$", subject, flags=re.I))


def _results_query(subject: str) -> bool:
    """True for a 📈 Results Radar request ('results', 'results radar', 'movers', 'reported')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:results?(?:\s*radar)?|movers?|reported|"
                         r"earnings\s*movers?)\s*[:\-]?\s*$", subject, flags=re.I))


def _wants_latest(subject: str) -> bool:
    """True if a Tailwind / Pickaxe / Hotlist request asks to bypass the 24h cache
    ('--latest' / 'fresh')."""
    return bool(re.search(r"(?:--?\s*)?\b(?:latest|fresh|refresh)\b", subject or "", flags=re.I))


def _investor_query(subject: str) -> str | None:
    """Parse 'investor: <name>' / 'hni: <name>' → the free-text name, or None."""
    m = re.match(r"^\s*(?:re:\s*)?(?:investor|hni)\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _help_query(subject: str) -> bool:
    """True for a bare help / command-menu request ('help', 'commands', 'menu', '?',
    'what can you do')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:help|help\s*me|commands?|command\s*list|menu|"
                         r"what\s*can\s*(?:you|i)\s*(?:do|ask)|\?)\s*[:\-]?\s*$",
                         subject, flags=re.I))


_AMOUNT_UNITS = {"k": 1e3, "thousand": 1e3, "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5,
                 "cr": 1e7, "crore": 1e7, "crores": 1e7}


def _raise_amount(subject: str) -> float | None:
    """'raise 50000' · 'sell ₹1.5 lakh' · 'take out 2L' · 'I wanna take out 50k' · 'need 3 lakh' → the ₹
    amount to raise from your holdings; None when it isn't such a request."""
    s = re.sub(r"^\s*re:\s*", "", subject or "", flags=re.I).strip()
    m = re.match(r"^(?:i\s+(?:want|wanna|need)\s+(?:to\s+)?)?(?:sell|raise|trim|take\s*out|withdraw|need|free\s*up)"
                 r"\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*"
                 r"(k|thousand|lacs?|lakhs?|l|crores?|cr)?\b", s, flags=re.I)
    if not m:
        return None
    amt = float(m.group(1).replace(",", "")) * _AMOUNT_UNITS.get((m.group(2) or "").lower(), 1)
    return amt if amt >= 1 else None


def _sell_query(subject: str) -> bool:
    """True for a holdings sell-priority request — bare 'sell' / 'raise' / 'trim' (optionally
    with trailing text, e.g. 'sell: need cash'). Ranks YOUR holdings weakest-hand first."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:sell|raise|trim)(?:\s*[:\-]\s*.*)?$",
                         subject, flags=re.I))


def _levels_query(subject: str) -> str | None:
    """Parse 'levels: <name>' / 'technical: <name>' / 'setup: <name>' / 'chart: <name>' →
    the free-text company name, or None. A quick, no-LLM technical read (support/resistance
    zones, structure, patterns, entry/stop/target) with an annotated chart."""
    m = re.match(r"^\s*(?:re:\s*)?(?:levels?|technicals?|setup|chart)\s*[:\-]\s*(.+)$",
                 subject, flags=re.I)
    return m.group(1).strip() if m and m.group(1).strip() else None


# ----------------- fund (mutual-fund) reports -----------------
class _FundCand:
    """Minimal Candidate shim so fund matches reuse the pending/disambiguation UX."""
    def __init__(self, scheme_code: int, name: str) -> None:
        self.symbol = f"MF:{scheme_code}"
        self.name = name


def _fund_query(subject: str) -> str | None:
    """Return the fund name if the subject is a fund request ('fund: X' / 'mf: X'), else None."""
    m = re.match(r"^\s*(?:re:\s*)?(?:fund|mf)\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m else None
