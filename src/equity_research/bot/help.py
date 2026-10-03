"""The `help` reply: every command, grouped (also the web UI's command list and autocomplete)."""

from __future__ import annotations

from equity_research.bot.core import _md_table, _re_subject, log
from equity_research.reports import email as emailer
from equity_research.reports.inbox import EmailRequest

# The complete command menu, section by section. Kept in one place so `help` never drifts from
# what handle_request actually dispatches. (cmd, what-you-get) rows; commands in `backticks`.
_HELP_SECTIONS: list[tuple[str, str, list[list[str]]]] = [
    ("📊 Stock deep report", "Just put a company name or NSE symbol in the Subject — the core report.", [
        ["`Infosys` (any company name or symbol)",
         "Full deep report: fundamentals, forensics (Altman / Beneish / Piotroski), sector-lens "
         "valuation, technicals, shareholding + smart-money cost & profit-booking risk, and an "
         "employee & management sentiment read (🏢 inside view) — with a PDF."],
        ["`Reliance consolidated` / `Reliance standalone`",
         "Same report, forced to that financials basis (default auto-picks)."],
        ["reply `1` after a report",
         "Upside Drivers 1-pager — forward catalysts, each with an estimated ₹cr / % business impact."],
    ]),
    ("💰 Your portfolio & holdings", "Reads your tagged holdings in the watchlist.", [
        ["`booking`", "Where the tracked institutions on YOUR holdings sit on big gains → "
                      "profit-booking (selling) risk, ranked."],
        ["`sell` (or `raise` / `trim`)",
         "Ranks your holdings weakest-hand-first — which to sell first if you need cash; with quantities "
         "(💼 My holdings) it also shows each one's value, profit / loss and short- / long-term."],
        ["`raise 50000` (or `take out 2 lakh` · `sell ₹1.5L` · `need 50k`)",
         "Exactly what to sell to raise that much: two plans — 🧾 least tax and 💪 weakest holdings first — "
         "with shares, ≈ money in hand and the estimated capital-gains tax (FIFO, set-off, the yearly "
         "₹1.25 lakh long-term exemption), plus 'wait N days and it turns long-term' tips. Needs your quantities "
         "in the web UI's 💼 My holdings — with a buy date, enter qty and price as you bought them (splits / "
         "bonuses since are applied); without one, what your broker shows today."],
    ]),
    ("🔎 Idea screeners — find new names", "Each returns a numbered list; reply a number → deep report.", [
        ["`screen: value` (or just `screen`)",
         "Quality + forensic + cheap-vs-own-history, ranked across the Nifty-500."],
        ["`screen: volume`", "Volume Breakouts — near 52-week highs, uptrend, confirmed by a volume surge."],
        ["`screen: beaters`", "Market Beaters — names beating the Nifty-500 over 3/6/12 months."],
        ["`screen: institutions`", "Institutional Buying — promoters raised their own stake QoQ (+ who's adding)."],
        ["`screen: margins`", "Margin Momentum — net margin expanding on growing revenue."],
        ["`screen: deleverage`", "Debt Payers — cut debt over 3-4 yrs while staying profitable."],
        ["`screen: quality`", "Compounders — high ROCE, low debt, steady multi-year growth, clean."],
        ["`screen: holdco`", "Holding companies trading below their listed-stake NAV (the Elcid trade)."],
        ["`screen: investors`", "Where marquee HNIs just entered / added / trimmed."],
        ["`screen: smallcap`", "Capex-led small-caps (spending now for future growth)."],
        ["`screen: technical`", "Strongest chart setups to buy — with entry / stop / target."],
        ["`screen: policy`", "Recent govt schemes/policies → likely listed beneficiaries."],
    ]),
    ("🔥 Hotlist — multi-signal confluence", "Cached 24h — add `--latest` to force a fresh run.", [
        ["`hotlist`",
         "The names lighting up across **several** discovery engines at once (momentum, leaders, "
         "accumulation, value, small-cap) — highest-conviction leads first. Reply a number → deep report."],
    ]),
    ("🎙️ Concalls — notable earnings calls", "Also pushed weekly (Sat ≥18:00).", [
        ["`concalls` (or `calls`)",
         "The most notable recent earnings calls market-wide: **Management Tone** (from the transcript) "
         "vs the quarter's **Execution** (from our numbers) — ranked by how far the two diverge (upbeat "
         "talk on soft numbers = caution; quiet talk on strong numbers = under-radar). "
         "Reply a number → deep report."],
    ]),
    ("📈 Results Radar — strongest just-reported quarters", "Also pushed weekly (Sat ≥18:00).", [
        ["`results` (or `movers`)",
         "Companies that **just reported**, ranked by how strong the quarter was — YoY growth + whether "
         "it's **accelerating** + margin inflection (from the numbers; no analyst consensus, so it's "
         "growth-vs-own-history). Reply a number → deep report."],
        ["`scorecard` (or `track record`)",
         "📊 **How this tool's own calls did** — every deep-report verdict and idea-engine pick, logged as "
         "it went out and scored vs the Nifty 500 (hit rate with confidence interval, excess return, "
         "best and worst), including the misses. Also emailed weekly."],
    ]),
    ("🔔 Announcements — get pinged on any filing", "Standing keyword alerts, pushed within ~20 min "
                                                    "(8am-11pm IST).", [
        ["`alert: <keyword>`",
         "Watch every company's exchange filings for a phrase — e.g. `alert: order win`, `alert: QIP`, "
         "`alert: capacity expansion`. You get an email the moment one files a match (forward-looking; "
         "use root words — ‘order’ also catches ‘orders’)."],
        ["`alerts`", "List your current alerts."],
        ["`unalert: <keyword>`", "Remove one (`alert clear` removes all)."],
    ]),
    ("🧭 Sector analysis", "Top-down, one sectoral index at a time.", [
        ["`sector: <name>` e.g. `sector: defence`, `sector: pharma`",
         "Trend + valuation vs its own history + who's accumulating + best & cheapest names + "
         "supply chain. Reply a number → deep report."],
        ["`sector: list`", "The ~20 sectors covered."],
        ["`sector: rotation`", "All sectors ranked — leaders / laggards / turning-up-from-cheap."],
    ]),
    ("👤 Marquee investors (HNIs)", "", [
        ["`investor: <name>` e.g. `investor: Mukul Agrawal`",
         "That investor's disclosed holdings + latest-quarter moves + their cost vs the current price."],
    ]),
    ("🔗 Supply chain", "", [
        ["`suppliers: <company>` e.g. `suppliers: BEL`",
         "The smaller LISTED suppliers / ancillaries feeding that name. Reply a number → deep report."],
    ]),
    ("💨 Global supply shocks (Tailwind)", "Cached for 24h — add `--latest` to force a fresh scan.", [
        ["`tailwind` (or `catalysts`)",
         "Global commodity shocks — export bans / quotas / tariffs (metals, agri, pharma, chemicals, "
         "energy…) → verified Indian beneficiaries, with the supplier's world-share and each firm's "
         "revenue-share & market share. Reply with a symbol → deep report."],
        ["`tailwind --latest` (or `tailwind fresh`)",
         "Same, but forces a brand-new live scan instead of the 24h-cached result."],
    ]),
    ("⛏️ Surging demand (Pickaxe)", "A deep build (~10-15 min) — you're acked instantly and the full "
                                    "report + charted PDF lands in-thread when ready. Cached 24h.", [
        ["`pickaxe` (or `demand`)",
         "India's rising demand (Google-Trends 'buy' searches + demand-surge news) → the indirect "
         "**'sell the pickaxes'** listed beneficiaries (feed/vaccine/ingredient/equipment names that "
         "ride a boom with less cyclicality) AND the direct plays. Each name carries **exact price / "
         "P/E-vs-sector / support-resistance** and a **filing-grounded revenue-share now → next-FY + "
         "growth, with sources**, plus a **Google-Trends chart** per theme (PDF). Reply a number → "
         "full deep report."],
        ["`pickaxe --latest`",
         "Same, but forces a brand-new live scan instead of the 24h-cached result."],
    ]),
    ("🔍 Reality Check — is that reel / post / article true?", "Paste a link or the text itself.", [
        ["`reality check: <link or text>` (or `reality: …`, `verify: …`)",
         "Reads a **news article, X post or Reddit post** (or text you paste — e.g. a reel's caption) → "
         "each claim checked against the company's **exchange filings** and reported numbers (✅ / 🟡 / ❌ / ⚠️), "
         "**how big** it is vs the company's revenue and market cap, whether it's **already in the price**, "
         "**red flags** (micro-cap, thin trading, run-up, pledges, promoter selling, hype words) and a bottom "
         "line. Reply a number → that company's deep report."],
    ]),
    ("🛡️ Thesis Guard — why you own it, re-checked every evening", "Emails you only when something changes.", [
        ["`thesis: <company> — <your reasons>; <your rules>`",
         "Write why you own a stock and, optionally, your rules — e.g. `thesis: BEL — order book keeps growing, "
         "debt-free, promoters not selling; exit below 250, trim above 450, trail 15%`. Each reason becomes a "
         "check computed from filings (growth, margins, ROE, debt, promoter / MF / FII stakes, pledge, P/E) or, "
         "where no number measures it, judged from recent filings and concall notes with the filing cited. "
         "🟢 intact · 🟡 weakening · 🔴 broken; rules show 🔔 when triggered."],
        ["`thesis: <company>` · `theses` · `unthesis: <company>`",
         "The full check for one · all of them · stop tracking one."],
    ]),
    ("🏛️ Government policy radar", "", [
        ["`policy` (or `schemes`)",
         "Recent official (PIB) schemes / policies → likely listed beneficiaries (watchlist flagged). "
         "Same as `screen: policy`."],
    ]),
    ("📈 Quick technical levels", "Computed, no LLM wait (~30s).", [
        ["`levels: <name>` (or `chart:` / `setup:`)",
         "Support / resistance zones, structure, patterns, entry / stop / target + an annotated chart."],
    ]),
    ("🟢 IPOs", "", [
        ["`ipo: ongoing` (or just `ipo`)", "IPOs open right now → reply a number → full analysis."],
        ["`ipo: upcoming`", "Upcoming IPOs whose RHP is out → reply a number → analysis."],
        ["`ipo: <name>`", "That IPO's note — price band, financials, fresh vs OFS, risks, anchors."],
    ]),
    ("💵 Mutual funds", "", [
        ["`fund: <name>` (or `mf: <name>`) e.g. `fund: Parag Parikh Flexi Cap`",
         "Fund report — returns, rolling performance, risk, top holdings — with a PDF. "
         "Several matches → a numbered menu."],
    ]),
    ("📬 Arrives automatically (no command needed)", "Scheduled pushes to your inbox.", [
        ["🌅 Pre-market (morning)", "GIFT Nifty implied Nifty open + overnight US/Asia + VIX / FII + news."],
        ["🔔 Midday (12:30)", "Live movers, filings today, insider trades."],
        ["📊 Full digest (18:00)", "Market header + watchlist alerts + events (with filing analysis) + insider."],
        ["📡 Screener movements (Sat 18:00)", "What newly crossed the screens this week."],
        ["🔄 Sector rotation (Sat 18:00)", "Sector leaders / laggards / value-turning."],
        ["💨 Tailwind (Sat + urgent pre-market/midday/evening)", "Global supply-shock → Indian beneficiaries."],
        ["⛏️ Pickaxe (monthly, 1st Sat 18:00)", "Surging Indian demand → indirect 'sell the pickaxes' beneficiaries."],
    ]),
]

_HELP_FOOTER = (
    "**Tips**\n\n"
    "- **Reply a number** to any list (screeners, sectors, suppliers, IPOs, funds) to drill into it.\n"
    "- Add **consolidated** or **standalone** after a stock name to force the financials basis.\n"
    "- A list's numbered menu stays answerable for **24h** in that email thread.\n"
    "- Not sure of a name? Just send your best guess — I resolve it (and ask if there are several)."
)


def _send_help(req: EmailRequest) -> None:
    """The full command menu — every option, section by section, each as a table."""
    log.info("sending help/command menu to %s", req.sender)
    parts = ["# 📖 Your command menu",
             "_Everything you can email me — put the command in the **Subject** line. "
             "Section by section below._"]
    for title, intro, rows in _HELP_SECTIONS:
        parts.append(f"## {title}")
        if intro:
            parts.append(f"_{intro}_")
        parts.append(_md_table(["Put this in the Subject", "What you get back"], rows, "ll"))
    parts.append("---")
    parts.append(_HELP_FOOTER)
    body = "\n\n".join(parts)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Commands"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
