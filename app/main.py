"""Telegram entry point for the Family Event Bot."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .assistant import HELP_TEXT, Assistant, Reply
from .calendars import make_backend
from .config import Config
from .llm import make_llm
from .store import Store
from .stt import STTClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("family-event-bot")

HEARTBEAT_INTERVAL = 60


def undo_markup(token: str | None) -> InlineKeyboardMarkup | None:
    if not token:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{token}")]])


async def send_replies(update: Update, replies: list[Reply]) -> None:
    for r in replies:
        await update.effective_message.reply_text(
            r.text, parse_mode=ParseMode.HTML, reply_markup=undo_markup(r.undo_token),
            disable_web_page_preview=True,
        )


def build_app(cfg: Config) -> Application:
    store = Store(cfg.data_dir / "state.sqlite3")
    calendar = make_backend(cfg)
    llm = make_llm(cfg.llm_provider, cfg.llm_api_key, cfg.llm_models, zdr=cfg.openrouter_zdr)
    llm.on_usage = store.add_usage
    stt = STTClient(cfg.stt_url) if cfg.stt_url else None
    assistant = Assistant(
        llm, calendar, store, cfg.tz, cfg.default_duration_minutes,
        stt=stt, voice_max_seconds=cfg.voice_max_seconds, local_places=cfg.local_places,
    )
    heartbeat_file = cfg.data_dir / "heartbeat"
    allowed = filters.User(user_id=list(cfg.allowed_user_ids))

    async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    async def on_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_replies(update, [assistant.cancel(update.effective_chat.id)])

    async def on_upcoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_replies(update, [await assistant.upcoming()])

    async def on_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send_replies(update, [assistant.usage()])

    async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        media = msg.voice or msg.audio
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        if (media.duration or 0) > cfg.voice_max_seconds or not stt:
            # Don't download what we won't transcribe.
            replies = await assistant.handle_voice(update.effective_chat.id, b"", media.duration or 0)
        else:
            tg_file = await media.get_file()
            audio = bytes(await tg_file.download_as_bytearray())  # kept in memory only
            replies = await assistant.handle_voice(update.effective_chat.id, audio, media.duration or 0)
        await send_replies(update, replies)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        replies = await assistant.handle(update.effective_chat.id, update.effective_message.text or "")
        await send_replies(update, replies)

    async def on_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.from_user.id not in cfg.allowed_user_ids:
            await q.answer()
            return
        token = (q.data or "").split(":", 1)[-1]
        text = await assistant.undo(q.message.chat.id, token)
        await q.answer("Undone" if text.startswith("↩️") else "")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def on_stranger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        # Do not respond; just log so the ID can be added to ALLOWED_USER_IDS if intended.
        log.warning("Ignored message from unapproved user id=%s username=%s", u and u.id, u and u.username)

    async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Unhandled error", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
            except Exception:
                pass

    async def heartbeat(app: Application) -> None:
        failures = 0
        while True:
            try:
                await app.bot.get_me()
                heartbeat_file.write_text(str(int(time.time())))
                failures = 0
            except Exception as e:
                failures += 1
                log.warning("heartbeat failed (%d): %s", failures, e)
                if failures >= 10:
                    log.error("Telegram unreachable for ~10 minutes; exiting so Docker restarts the bot")
                    os._exit(1)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def post_init(app: Application) -> None:
        await asyncio.to_thread(calendar.check)
        await app.bot.set_my_commands([
            BotCommand("upcoming", "List the next events"),
            BotCommand("cancel", "Cancel the current conversation and start over"),
            BotCommand("usage", "Which AI model answered and estimated cost"),
            BotCommand("help", "How to use the bot"),
        ])
        app.bot_data["heartbeat"] = asyncio.create_task(heartbeat(app))
        me = await app.bot.get_me()
        log.info("Started as @%s using %s, models=%s", me.username, calendar.name, cfg.llm_models)
        if cfg.notify_on_start:
            for uid in cfg.admin_user_ids:
                try:
                    await app.bot.send_message(uid, f"✅ Family Event Bot (re)started — writing to {calendar.name}.")
                except Exception as e:
                    log.warning("could not notify %s: %s", uid, e)

    async def post_shutdown(app: Application) -> None:
        task = app.bot_data.get("heartbeat")
        if task:
            task.cancel()
        await llm.aclose()
        if stt:
            await stt.aclose()

    app = (
        ApplicationBuilder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], on_start, filters=allowed))
    app.add_handler(CommandHandler("cancel", on_cancel, filters=allowed))
    app.add_handler(CommandHandler("upcoming", on_upcoming, filters=allowed))
    app.add_handler(CommandHandler("usage", on_usage, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(allowed & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_undo, pattern=r"^undo:"))
    app.add_handler(MessageHandler(~allowed, on_stranger))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    cfg = Config.from_env()
    build_app(cfg).run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
