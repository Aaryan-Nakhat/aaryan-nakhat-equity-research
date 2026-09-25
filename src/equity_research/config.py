"""Central configuration — the single source of truth for every tunable knob.

Everything here reads from an environment variable (typically a ``.env`` file, loaded by the
entrypoints) and falls back to a sensible **default equal to the project's original behaviour**, so a
fresh clone runs identically with zero configuration. Other modules import from here rather than
scattering ``os.environ`` reads and magic numbers across the codebase::

    from equity_research import config
    if now.hour >= config.EOD_HOUR: ...

Two documentation tiers (both equally overridable — the split is only how prominently they appear in
``.env.example``):
  * **Tier 1** — the schedule, feature toggles, timezone, cache TTLs, main timeouts, market-cap bands
    and headline tuning a normal self-hoster changes. Documented up top in ``.env.example``.
  * **Tier 2** — deep analytical thresholds (screen liquidity floors, engine depths, projection
    horizons). Centralised here, env-overridable, listed under an "Advanced" section.

This is an **India (NSE/BSE) build**: benchmarks (Nifty), the ₹-crore scale, the data sources
(NSE/BSE/PIB/AMFI/FBIL/MCX) and SEBI-disclosure concepts are structural, not configuration. The
``TIMEZONE`` var mainly shifts *delivery* times; it does not turn this into a different market's tool.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

_TRUTHY = {"1", "true", "yes", "on"}
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ── typed env readers (blank / unset / unparseable → default) ──
def env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v.strip()) if v and v.strip() else default
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v.strip()) if v and v.strip() else default
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in _TRUTHY


def env_csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name)
    raw = raw if raw is not None else default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _parse_hm(text: str) -> tuple[int, int] | None:
    """'HH:MM' → (h, m); None if malformed (skipped by callers)."""
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def env_time(name: str, default: str) -> tuple[int, int]:
    return _parse_hm(env_str(name, default)) or _parse_hm(default)  # type: ignore[return-value]


def env_times(name: str, default: str) -> list[tuple[int, int]]:
    """Comma-separated 'HH:MM' list → [(h, m), …]. An explicitly empty value → [] (feature off)."""
    raw = os.environ.get(name)
    raw = raw if raw is not None else default
    return [hm for x in raw.split(",") if (hm := _parse_hm(x))]


def env_weekday(name: str, default: str) -> int:
    """Day name (Mon/Tue/…/Sun, case-insensitive) or 0–6 → weekday int (Mon=0 … Sun=6)."""
    v = env_str(name, default).lower()
    if v in _WEEKDAYS:
        return _WEEKDAYS[v]
    try:
        n = int(v)
        return n if 0 <= n <= 6 else _WEEKDAYS.get(default.lower(), 5)
    except ValueError:
        return _WEEKDAYS.get(default.lower(), 5)


# ══════════════════════════════════════════════════════════════════════════
# TIER 1 — the headline config
# ══════════════════════════════════════════════════════════════════════════

# ── Timezone ──
TZ_NAME = env_str("TIMEZONE", "Asia/Kolkata")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001 — bad tz name → fall back rather than crash the bot
    TZ_NAME, TZ = "Asia/Kolkata", ZoneInfo("Asia/Kolkata")

# ── Delivery schedule (local time in TZ) ──
EOD_HOUR = env_int("PUSH_EOD_HOUR", 18)                    # full daily digest + all weekly pushes
PREMARKET = env_time("PUSH_PREMARKET", "08:30")           # pre-open digest (h, m)
PREMARKET_CUTOFF_HOUR = env_int("PUSH_PREMARKET_CUTOFF_HOUR", 12)
MIDDAY = env_time("PUSH_MIDDAY", "12:30")                 # midday digest (h, m)
MIDDAY_CUTOFF_HOUR = env_int("PUSH_MIDDAY_CUTOFF_HOUR", 14)
WEEKLY_PUSH_WEEKDAY = env_weekday("WEEKLY_PUSH_DAY", "Sat")   # which weekday weekly pushes fire on
HEARTBEAT_SECONDS = env_int("HEARTBEAT_SECONDS", 300)     # IMAP IDLE wait + scheduler heartbeat
MENU_TTL_HOURS = env_int("MENU_TTL_HOURS", 24)            # how long a numbered "which one?" menu lasts
RECONNECT_BACKOFF_S = env_int("RECONNECT_BACKOFF_S", 15)  # backoff after an IMAP session error

# The three (or user-set) urgent-Tailwind break-in slots. Empty value → urgent alerts disabled.
_URGENT_FRIENDLY = {PREMARKET: "pre-market", MIDDAY: "midday", (EOD_HOUR, 0): "evening"}


def _slot_label(h: int, m: int) -> str:
    return _URGENT_FRIENDLY.get((h, m), f"{h:02d}:{m:02d}")


URGENT_SLOTS = [(h, m, _slot_label(h, m))
                for (h, m) in env_times("TAILWIND_URGENT_SLOTS", "08:30,12:30,18:00")]

# ── Mailbox housekeeping ──
MAIL_BIN_AFTER_MIN = env_int("MAIL_BIN_AFTER_MIN", 30)
MAIL_SWEEP_EVERY_MIN = env_int("MAIL_SWEEP_EVERY_MIN", 15)

# ── Feature toggles (per scheduled push; all default on) ──
ENABLE_PREMARKET = env_bool("ENABLE_PREMARKET", True)
ENABLE_MIDDAY = env_bool("ENABLE_MIDDAY", True)
ENABLE_EOD_DIGEST = env_bool("ENABLE_EOD_DIGEST", True)
ENABLE_SCREEN_DIGEST = env_bool("ENABLE_SCREEN_DIGEST", True)
ENABLE_SECTOR_ROTATION = env_bool("ENABLE_SECTOR_ROTATION", True)
ENABLE_TAILWIND = env_bool("ENABLE_TAILWIND", True)
ENABLE_PICKAXE = env_bool("ENABLE_PICKAXE", True)
ENABLE_CONCALLS = env_bool("ENABLE_CONCALLS", True)
ENABLE_RESULTS_RADAR = env_bool("ENABLE_RESULTS_RADAR", True)
ENABLE_KEYWORD_ALERTS = env_bool("ENABLE_KEYWORD_ALERTS", True)
ENABLE_MAIL_HOUSEKEEPING = env_bool("ENABLE_MAIL_HOUSEKEEPING", True)

# ── Cache / dedup TTLs ──
TAILWIND_SEEN_TTL_DAYS = env_int("TAILWIND_SEEN_TTL_DAYS", 14)
TAILWIND_CACHE_TTL_H = env_int("TAILWIND_CACHE_TTL_H", 24)
HOTLIST_CACHE_TTL_H = env_int("HOTLIST_CACHE_TTL_H", 24)
PICKAXE_CACHE_TTL_H = env_int("PICKAXE_CACHE_TTL_H", 24)
EMPLOYER_CACHE_TTL_DAYS = env_int("EMPLOYER_CACHE_TTL_DAYS", 30)

# ── Timeouts (seconds) on best-effort subprocesses ──
PDF_RENDER_TIMEOUT_S = env_int("PDF_RENDER_TIMEOUT_S", 150)
LEVELS_PDF_TIMEOUT_S = env_int("LEVELS_PDF_TIMEOUT_S", 90)
SCREEN_TIMEOUT_S = env_int("SCREEN_TIMEOUT_S", 300)
TAILWIND_TIMEOUT_S = env_int("TAILWIND_TIMEOUT_S", 420)
HOTLIST_TIMEOUT_S = env_int("HOTLIST_TIMEOUT_S", 420)
POLICY_TIMEOUT_S = env_int("POLICY_TIMEOUT_S", 300)
HTTP_TIMEOUT_S = env_int("HTTP_TIMEOUT_S", 30)

# ── Market-cap bands (₹ crore) ──
SMALLCAP_MAX_CR = env_int("SMALLCAP_MAX_CR", 5_000)
MIDCAP_MAX_CR = env_int("MIDCAP_MAX_CR", 25_000)
LARGECAP_MAX_CR = env_int("LARGECAP_MAX_CR", 75_000)
SIZE_BANDS = [(SMALLCAP_MAX_CR, "small-cap"), (MIDCAP_MAX_CR, "mid-cap"), (LARGECAP_MAX_CR, "large-cap")]
TAILWIND_SMALLMID_CEIL_CR = env_int("TAILWIND_SMALLMID_CEIL_CR", MIDCAP_MAX_CR)

# ── Headline tuning ──
RISK_FREE_RATE = env_float("RISK_FREE_RATE", 0.07)        # DCF / CAPM / fund-Sharpe (unified)
BIG_MOVE_PCT = env_float("BIG_MOVE_PCT", 0.06)            # |1-day return| that fires a big-move alert


# ══════════════════════════════════════════════════════════════════════════
# TIER 2 — advanced tuning (rarely changed; centralised + env-overridable)
# ══════════════════════════════════════════════════════════════════════════

# ── Background-pass cadence gaps (minutes) + the keyword-alert active window (local hours) ──
ALERT_ACTIVE_START_HOUR = env_int("ALERT_ACTIVE_START_HOUR", 8)
ALERT_ACTIVE_END_HOUR = env_int("ALERT_ACTIVE_END_HOUR", 23)    # exclusive upper bound (range())
ALERT_SCAN_GAP_MIN = env_int("ALERT_SCAN_GAP_MIN", 20)
CONCALL_INGEST_GAP_MIN = env_int("CONCALL_INGEST_GAP_MIN", 60)
RESULTS_INGEST_GAP_MIN = env_int("RESULTS_INGEST_GAP_MIN", 60)

# ── Result caps / list sizes / batch bounds ──
SCREEN_RESULT_LIMIT = env_int("SCREEN_RESULT_LIMIT", 20)   # default rows for most screen commands
TECHNICAL_LIMIT = env_int("TECHNICAL_LIMIT", 15)
HOTLIST_LIMIT = env_int("HOTLIST_LIMIT", 25)
HOLDCO_LIMIT = env_int("HOLDCO_LIMIT", 30)
CALL_RADAR_MAX_NEW = env_int("CALL_RADAR_MAX_NEW", 8)      # transcripts scored per background pass
RESULTS_MAX_NEW = env_int("RESULTS_MAX_NEW", 6)            # names refreshed per background pass
TAILWIND_MAX_CATALYSTS = env_int("TAILWIND_MAX_CATALYSTS", 8)
TAILWIND_URGENT_MAX_CATALYSTS = env_int("TAILWIND_URGENT_MAX_CATALYSTS", 2)
TAILWIND_URGENT_MAP_CAP = env_int("TAILWIND_URGENT_MAP_CAP", 6)
PICKAXE_MAX_THEMES = env_int("PICKAXE_MAX_THEMES", 6)
PICKAXE_DAYS = env_int("PICKAXE_DAYS", 21)                 # demand-trend lookback window
TRENDS_PER_CAT = env_int("TRENDS_PER_CAT", 10)
TRENDS_MAX_SIGNALS = env_int("TRENDS_MAX_SIGNALS", 40)
NEWS_MAX_SIGNALS = env_int("NEWS_MAX_SIGNALS", 70)
POLICY_MAX_CLASSIFY = env_int("POLICY_MAX_CLASSIFY", 55)
POLICY_LIMIT_RELEASES = env_int("POLICY_LIMIT_RELEASES", 120)
HOTLIST_ENGINE_DEPTH = env_int("HOTLIST_ENGINE_DEPTH", 30)
ACCUM_ENRICH_CAP = env_int("ACCUM_ENRICH_CAP", 40)
SHORTLIST_CAP = env_int("SHORTLIST_CAP", 60)               # momentum/leaders/technical trap-gate depth

# ── Screen eligibility bands (₹ crore) ──
SMALLCAP_BAND_LO_CR = env_float("SMALLCAP_BAND_LO_CR", 1_000.0)
SMALLCAP_BAND_HI_CR = env_float("SMALLCAP_BAND_HI_CR", 10_000.0)
HOLDCO_MIN_NAV_CR = env_float("HOLDCO_MIN_NAV_CR", 500.0)

# ── Signal / liquidity / momentum thresholds ──
MOMENTUM_MIN_TURNOVER_CR = env_float("MOMENTUM_MIN_TURNOVER_CR", 2.0)
MOMENTUM_MIN_DAYS = env_int("MOMENTUM_MIN_DAYS", 150)
MOMENTUM_NEAR_HIGH = env_float("MOMENTUM_NEAR_HIGH", -0.04)   # within 4% of the 52-week high
MOMENTUM_MIN_VOL_SURGE = env_float("MOMENTUM_MIN_VOL_SURGE", 1.3)
MOMENTUM_LIQ_WINDOW = env_int("MOMENTUM_LIQ_WINDOW", 20)
LEADERS_MIN_TURNOVER_CR = env_float("LEADERS_MIN_TURNOVER_CR", 10.0)
LEADERS_LIQ_WINDOW = env_int("LEADERS_LIQ_WINDOW", 20)
ACCUM_MIN_PROMOTER_DELTA = env_float("ACCUM_MIN_PROMOTER_DELTA", 0.10)
OWNERSHIP_APPEAR_FLOOR = env_float("OWNERSHIP_APPEAR_FLOOR", 0.20)
OWNERSHIP_DELTA_FLOOR = env_float("OWNERSHIP_DELTA_FLOOR", 0.10)
CALL_RADAR_LOOKBACK_DAYS = env_int("CALL_RADAR_LOOKBACK_DAYS", 45)
CALL_RADAR_WINDOW_DAYS = env_int("CALL_RADAR_WINDOW_DAYS", 35)
CALL_RADAR_GAP_THRESHOLD = env_int("CALL_RADAR_GAP_THRESHOLD", 2)
RESULTS_LOOKBACK_DAYS = env_int("RESULTS_LOOKBACK_DAYS", 45)
RESULTS_FRESH_DAYS = env_int("RESULTS_FRESH_DAYS", 35)
RESULTS_STALE_DAYS = env_int("RESULTS_STALE_DAYS", 100)
KEYWORD_SWEEP_DAYS = env_int("KEYWORD_SWEEP_DAYS", 3)

# ── Bank health checks (deep report §9 for banks; all in %) ──
BANK_NNPA_WARN = env_float("BANK_NNPA_WARN", 2.0)          # net NPA above this = elevated bad loans
BANK_NNPA_ALARM = env_float("BANK_NNPA_ALARM", 4.0)
BANK_PCR_WARN = env_float("BANK_PCR_WARN", 60.0)           # provision coverage below this = thin cushion
BANK_CET1_WARN = env_float("BANK_CET1_WARN", 10.0)         # RBI floor incl. conservation buffer is 8%
BANK_CD_RATIO_WARN = env_float("BANK_CD_RATIO_WARN", 90.0) # loans outrunning deposits = funding strain
BANK_ROA_WEAK = env_float("BANK_ROA_WEAK", 0.5)
BANK_ROA_STRONG = env_float("BANK_ROA_STRONG", 1.5)
BANK_CREDIT_COST_SPIKE_X = env_float("BANK_CREDIT_COST_SPIKE_X", 1.5)  # vs its own prior-years average
BANK_NII_LAG_PP = env_float("BANK_NII_LAG_PP", 5.0)        # NII growth this far below loan growth = NIM squeeze

# ── Projection / fund knobs ──
PROJ_YEARS = env_int("PROJ_YEARS", 10)                    # DCF projection horizon
FUNDS_TRADING_DAYS = env_int("FUNDS_TRADING_DAYS", 252)
FUNDS_ASOF_TOL_DAYS = env_int("FUNDS_ASOF_TOL_DAYS", 12)
FUNDS_SIP_MONTHLY = env_float("FUNDS_SIP_MONTHLY", 10_000.0)

# India-market benchmarks (structural, but overridable for a fork on another index family).
MARKET_INDEX = env_str("MARKET_INDEX", "Nifty 50")
LEADERS_BENCHMARK = env_str("LEADERS_BENCHMARK", "Nifty 500")
