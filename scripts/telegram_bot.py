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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from equity_research.reports import resolve as resolver
from equity_research.reports.pipeline import generate_report

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("equitybot")

_ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(" ", "").split(",") if x}
_TG_LIMIT = 4000


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error: %s", context.error)


def _authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in _ALLOWED)


def _chunks(text: str):
    for i in range(0, len(text), _TG_LIMIT):
        yield text[i:i + _TG_LIMIT]


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
    _, symbol, flag = q.data.split("|")
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

    # Inline: just the analysis section (chunked). File: the full brief + analysis.
    analysis = report.split("## Analysis", 1)[-1].strip() if "## Analysis" in report else report
    for chunk in _chunks(analysis):
        await chat.send_message(chunk)
    bio = io.BytesIO(report.encode("utf-8"))
    bio.name = f"{symbol}_{label}_report.md"
    await chat.send_document(document=bio, filename=bio.name,
                             caption=f"✅ {symbol} — full report ({label})")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set (see .env.example)")
    if not _ALLOWED:
        raise SystemExit("TELEGRAM_ALLOWED_USERS not set — refusing to run open to everyone")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_select, pattern=r"^an\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Bot running. Authorised users: %s", sorted(_ALLOWED))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
