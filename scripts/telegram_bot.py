"""Telegram bot — message a company name, get a deep equity report back.

Flow: you send "Adani Power" (optionally "... consolidated") -> the bot resolves
it to NSE symbol(s) [local map, else Gemini+Google-Search]; if several match it
shows buttons to pick one; then it ingests-on-demand, builds the deep brief,
runs the Gemini forensic analysis, and replies with the report (inline + a .md
file).

Only whitelisted Telegram user IDs are served. Run:

    set -a; . ./.env; set +a
    uv run python scripts/telegram_bot.py

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS (comma-separated numeric IDs)
plus the Gemini/Vertex vars. See .env.example.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import telegramify_markdown as tmd
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from equity_research import scan, watchlist
from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.reports import charts
from equity_research.reports import resolve as resolver
from equity_research.reports.pdf import report_to_pdf
from equity_research.reports.pipeline import generate_report


def _pdf_with_charts(symbol: str, report: str, title: str, consolidated: bool = False) -> bytes:
    """Full report PDF with fundamental charts embedded (charts best-effort)."""
    con = connect()
    try:
        images = charts.report_charts(con, symbol, consolidated)
    except Exception:  # noqa: BLE001 — a chart should never block the report
        log.exception("charts failed for %s", symbol)
        images = []
    finally:
        con.close()
    return report_to_pdf(report, title, images=images)

IST = ZoneInfo("Asia/Kolkata")
SCAN_TIME = dtime(18, 0, tzinfo=IST)

_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "processed", "telegram_bot.log")
os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(_LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("equitybot")

_ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(" ", "").split(",") if x}
_TG_LIMIT = 4000


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error: %s", context.error)


def _authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in _ALLOWED)


def _md_chunks(text: str, limit: int = 3500):
    """Split markdown on paragraph/heading boundaries into <=limit pieces so each
    converted chunk is self-contained valid MarkdownV2."""
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > limit:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


async def _send_markdown(chat, text: str) -> None:
    """Send markdown rendered for Telegram (MarkdownV2), with plain-text fallback."""
    for chunk in _md_chunks(text):
        try:
            await chat.send_message(tmd.markdownify(chunk), parse_mode="MarkdownV2")
        except Exception:  # noqa: BLE001 — bad escape shouldn't drop the report
            log.warning("MarkdownV2 send failed; falling back to plain text")
            await chat.send_message(chunk)


async def _send_md_text(bot, chat_id: int, text: str) -> None:
    """Send one short markdown message to a chat id (MarkdownV2, plain fallback)."""
    try:
        await bot.send_message(chat_id, tmd.markdownify(text), parse_mode="MarkdownV2")
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, text)


async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.message.reply_text("Not authorised.")
        return
    await update.message.reply_text(
        "Send a company name to get a deep fundamental + forensic report.\n"
        "Add 'consolidated' for the group view, e.g. `Reliance consolidated`.")


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("message from user %s: %r", update.effective_user.id if update.effective_user else "?",
             update.message.text)
    if not _authorized(update):
        await update.message.reply_text("Not authorised.")
        return
    query = (update.message.text or "").strip()
    consolidated = "consolidated" in query.lower()
    query = query.lower().replace("consolidated", "").strip()
    if not query:
        await update.message.reply_text("Send a company name.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    cands = await asyncio.to_thread(resolver.resolve, query)

    if not cands:
        await update.message.reply_text(f"Couldn't resolve “{query}” to an NSE symbol.")
        return
    if len(cands) == 1:
        await _run(update, cands[0].symbol, consolidated)
        return
    flag = "1" if consolidated else "0"
    buttons = [[InlineKeyboardButton(f"{c.name} ({c.symbol})",
                                     callback_data=f"an|{c.symbol}|{flag}")] for c in cands[:5]]
    await update.message.reply_text("Multiple matches — pick one:",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def on_select(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _authorized(update):
        return
    parts = q.data.split("|")
    if parts[0] == "wl":                         # add to watchlist
        symbol = parts[1]
        await q.edit_message_text(f"Adding {symbol} to watchlist…")
        await _do_watch(q.message.chat, symbol, "")
        return
    _, symbol, flag = parts                       # analyse now
    await q.edit_message_text(f"Selected {symbol}.")
    await _run(q, symbol, flag == "1")


async def _run(target, symbol: str, consolidated: bool) -> None:
    """Generate the report and reply. `target` is a Message or CallbackQuery."""
    chat = target.message.chat if hasattr(target, "message") else target.chat
    label = "consolidated" if consolidated else "standalone"
    await chat.send_message(f"⏳ Analysing {symbol} ({label}) — this takes ~1-2 min…")
    await chat.send_action(ChatAction.TYPING)
    try:
        report = await asyncio.to_thread(generate_report, symbol,
                                         deep=True, consolidated=consolidated)
    except Exception as e:  # noqa: BLE001
        await chat.send_message(f"❌ Failed for {symbol}: {type(e).__name__}: {e}")
        return

    # Inline: the analysis section, rendered. Attachment: the full report as PDF.
    analysis = report.split("## Analysis", 1)[-1].strip() if "## Analysis" in report else report
    await _send_markdown(chat, analysis)
    try:
        pdf = await asyncio.to_thread(_pdf_with_charts, symbol, report, f"{symbol} ({label})", consolidated)
        bio = io.BytesIO(pdf)
        bio.name = f"{symbol}_{label}_report.pdf"
    except Exception:  # noqa: BLE001 — fall back to the markdown text file
        log.exception("PDF render failed; sending markdown")
        bio = io.BytesIO(report.encode("utf-8"))
        bio.name = f"{symbol}_{label}_report.md"
    await chat.send_document(document=bio, filename=bio.name,
                             caption=f"✅ {symbol} — full report ({label})")


# ----------------- watchlist commands -----------------
async def _do_watch(chat, symbol: str, company: str) -> None:
    con = connect()
    try:
        watchlist.add(con, symbol, company)
        ok = await asyncio.to_thread(watchlist.ensure_data, con, symbol)
        await asyncio.to_thread(alerts.scan_symbol, con, symbol, [])   # seed state silently
    finally:
        con.close()
    tail = "" if ok else " (no NSE financials — price/announcement alerts only)"
    await chat.send_message(f"✅ Watching {symbol}{tail}")


async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Usage: /watch <company name>")
        return
    cands = await asyncio.to_thread(resolver.resolve, name)
    if not cands:
        await update.message.reply_text(f"Couldn't resolve “{name}”.")
        return
    if len(cands) == 1:
        await _do_watch(update.message.chat, cands[0].symbol, cands[0].name)
        return
    buttons = [[InlineKeyboardButton(f"{c.name} ({c.symbol})", callback_data=f"wl|{c.symbol}")]
               for c in cands[:5]]
    await update.message.reply_text("Pick one to watch:",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def unwatch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    sym = (ctx.args[0].upper() if ctx.args else "")
    if not sym:
        await update.message.reply_text("Usage: /unwatch <SYMBOL>")
        return
    con = connect()
    try:
        watchlist.remove(con, sym)
    finally:
        con.close()
    await update.message.reply_text(f"Removed {sym} from watchlist.")


async def watchlist_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    con = connect()
    try:
        ents = watchlist.entries(con)
    finally:
        con.close()
    if not ents:
        await update.message.reply_text("Watchlist is empty. Add with /watch <name>.")
        return
    lines = "\n".join(f"• {s} — {c}" if c else f"• {s}" for s, c in ents)
    await update.message.reply_text(f"Watchlist ({len(ents)}):\n{lines}")


# ----------------- scan + push -----------------
async def _push_scan(bot, chat_id: int, results: dict, movers: list) -> None:
    today = datetime.now(IST).date().isoformat()
    md = scan.format_digest(today, results, movers or [])
    await _send_md_text(bot, chat_id, md)


async def scan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text("🔎 Running watchlist scan now (may take a few min)…")
    results, movers = await asyncio.to_thread(scan.run_watchlist_scan)
    await _push_scan(ctx.bot, update.effective_chat.id, results, movers)


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the watchlist scan + push + mark today done. (Gating is in maybe_scan.)"""
    chat_id = min(_ALLOWED)
    log.info("watchlist scan starting")
    try:
        results, movers = await asyncio.to_thread(scan.run_watchlist_scan)
    except Exception:  # noqa: BLE001
        log.exception("scan failed")
        return
    await _push_scan(context.bot, chat_id, results, movers)
    await asyncio.to_thread(scan.mark_scanned)


async def maybe_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-healing daily gate (runs every 30 min). Fires the scan once per
    trading day, the first time the bot is up at/after 18:00 IST — robust to
    sleep, restarts, and late starts. Skips weekends/holidays."""
    now = datetime.now(IST)
    if now.hour < SCAN_TIME.hour:                       # before 18:00 IST
        return
    if await asyncio.to_thread(scan.already_scanned_today):
        return
    if not await asyncio.to_thread(scan.market_open_today):
        await asyncio.to_thread(scan.mark_scanned)       # holiday/weekend: mark, no push
        log.info("market closed today — skipping scan (marked done)")
        return
    log.info("self-healing daily scan firing")
    await scan_job(context)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set (see .env.example)")
    if not _ALLOWED:
        raise SystemExit("TELEGRAM_ALLOWED_USERS not set — refusing to run open to everyone")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CallbackQueryHandler(on_select, pattern=r"^(an|wl)\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    # Self-healing daily scan: a check every 30 min runs it once per trading day
    # the first time the bot is up at/after 18:00 IST (robust to sleep/restart).
    app.job_queue.run_repeating(maybe_scan, interval=1800, first=15)
    log.info("Bot running. Authorised users: %s · daily scan ~18:00 IST (self-healing)", sorted(_ALLOWED))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
