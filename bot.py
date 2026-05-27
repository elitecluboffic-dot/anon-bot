import logging
import os
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from dotenv import load_dotenv
from src.db import init_db
from src.handlers import (
    cmd_start, cmd_find, cmd_next, cmd_stop,
    cmd_profile, cmd_filter, cmd_invisible,
    cmd_premium, cmd_stats, cmd_help,
    cmd_addpremium, cmd_removepremium,
    cmd_backup, cmd_restore,
    cmd_warn, cmd_ban, cmd_unban, cmd_userinfo, cmd_reports,
    handle_callback, handle_message, error_handler,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

OWNER_ID = int(os.getenv("OWNER_ID", 0))


async def daily_backup(ctx):
    try:
        from src.backup import do_backup
        filepath = do_backup()
        filename = os.path.basename(filepath)
        if OWNER_ID:
            with open(filepath, "rb") as f:
                await ctx.bot.send_document(
                    chat_id=OWNER_ID,
                    document=f,
                    filename=filename,
                    caption=f"🔄 *Auto backup harian*\n`{filename}`",
                    parse_mode="Markdown",
                )
        logger.info(f"Auto backup selesai: {filename}")
    except Exception as e:
        logger.error(f"Auto backup gagal: {e}")


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN tidak ditemukan!")

    init_db()

    app = ApplicationBuilder().token(token).build()

    # ── Scheduler ─────────────────────────────────────
    app.job_queue.run_daily(daily_backup, time=__import__("datetime").time(hour=2, minute=0))

    # ── User Commands ──────────────────────────────────
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("find",          cmd_find))
    app.add_handler(CommandHandler("next",          cmd_next))
    app.add_handler(CommandHandler("stop",          cmd_stop))
    app.add_handler(CommandHandler("profile",       cmd_profile))
    app.add_handler(CommandHandler("filter",        cmd_filter))
    app.add_handler(CommandHandler("invisible",     cmd_invisible))
    app.add_handler(CommandHandler("premium",       cmd_premium))
    app.add_handler(CommandHandler("stats",         cmd_stats))
    app.add_handler(CommandHandler("help",          cmd_help))

    # ── Admin Commands ─────────────────────────────────
    app.add_handler(CommandHandler("addpremium",    cmd_addpremium))
    app.add_handler(CommandHandler("removepremium", cmd_removepremium))
    app.add_handler(CommandHandler("backup",        cmd_backup))
    app.add_handler(CommandHandler("restore",       cmd_restore))
    app.add_handler(CommandHandler("warn",          cmd_warn))
    app.add_handler(CommandHandler("ban",           cmd_ban))
    app.add_handler(CommandHandler("unban",         cmd_unban))
    app.add_handler(CommandHandler("userinfo",      cmd_userinfo))
    app.add_handler(CommandHandler("reports",       cmd_reports))

    # ── Callbacks & Messages ───────────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot Anonymous Chat berjalan...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
