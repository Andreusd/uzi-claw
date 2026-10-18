"""Telegram bot handlers, orchestration, and application setup for UziClaw."""

import logging
import sys
from telegram import (
    BotCommandScopeChat,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from .config import TELEGRAM_TOKEN, AGY_BIN_PATH, INJECT_TELEGRAM_CONTEXT, MEDIA_DIR
from .auth import restricted, load_authorized_users
from .session_manager import session_manager, SessionState
from .formatters import (
    split_message,
    safe_reply,
    safe_edit,
    format_usage_data,
    format_terminal_output,
    RateLimitedMessageUpdater,
)
from .antigravity_runner import get_usage
from .device_stats import format_device_stats
from .scheduler import (
    schedule_store,
    scheduler_service,
    format_timestamp,
    compute_next_run,
    parse_relative_time,
)
from .media_manager import cleanup_old_media, download_telegram_file_with_retry
from .handlers import (
    get_session_from_update,
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_usage,
    cmd_id,
    cmd_new,
    cmd_cancel,
    cmd_interrupt,
    cmd_device,
    cmd_stats,
    cmd_cd,
    cmd_autopilot,
    cmd_mode,
    cmd_recent,
    cmd_resume,
    cmd_restart,
    cmd_shutdown,
    handle_unknown_command,
    SuffixCommandHandler,
    ResumeCommandHandler,
    BOT_COMMANDS,
    cmd_schedules,
    cmd_schedule,
    cmd_cancel_schedule,
    cmd_run_schedule,
    handle_schedule_callback,
    build_prompt_with_context,
    prepare_prompt_from_message,
    prepare_prompt_from_messages,
    cmd_terminal,
    execute_terminal_command,
    handle_message,
)

logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)


async def post_init(app: Application) -> None:
    """Register command autosuggestions scoped strictly to authorized users, clean media, and start scheduler."""
    try:
        cleanup_old_media()
    except Exception as e:
        logger.debug("Failed initial media cleanup: %s", e)

    try:
        # Start background task scheduler
        scheduler_service.start(app.bot, session_manager)
    except Exception as e:
        logger.error("Failed to start scheduler service: %s", e)

    try:
        # Clear default/global commands so unauthorized users see no command menu
        await app.bot.delete_my_commands()

        users = load_authorized_users()
        for u in users:
            try:
                chat_id = int(u)
                await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id))
                logger.info("Registered scoped commands for authorized chat_id=%s", chat_id)
            except ValueError:
                pass
    except Exception as e:
        logger.warning("Failed to register scoped bot commands: %s", e)


async def post_shutdown(app: Application) -> None:
    """Graceful shutdown hook for UziClaw bot."""
    logger.info("Executing post_shutdown cleanup...")
    try:
        from .tray import stop_tray
        stop_tray()
    except Exception as e:
        logger.debug("Error stopping tray in post_shutdown: %s", e)
    try:
        await scheduler_service.stop()
    except Exception as e:
        logger.error("Error stopping scheduler service in post_shutdown: %s", e)


def create_application() -> Application:
    """Build and configure the Telegram application."""
    token = TELEGRAM_TOKEN
    bot_mod = sys.modules.get("src.bot")
    if bot_mod and hasattr(bot_mod, "TELEGRAM_TOKEN"):
        token = getattr(bot_mod, "TELEGRAM_TOKEN")

    if not token:
        raise ValueError("TELEGRAM_TOKEN is not set in environment or .env file.")

    load_auth_fn = load_authorized_users
    if bot_mod and hasattr(bot_mod, "load_authorized_users"):
        load_auth_fn = getattr(bot_mod, "load_authorized_users")
    load_auth_fn()

    request_cfg = HTTPXRequest(
        connection_pool_size=32,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=20.0,
        media_write_timeout=60.0,
    )
    get_updates_cfg = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=15.0,
        read_timeout=60.0,
        write_timeout=20.0,
    )

    app = (
        Application.builder()
        .token(token)
        .request(request_cfg)
        .get_updates_request(get_updates_cfg)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("agendamentos", cmd_schedules))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("agendar", cmd_schedule))
    app.add_handler(SuffixCommandHandler("cancel_schedule", cmd_cancel_schedule))
    app.add_handler(SuffixCommandHandler("cancelar_agendamento", cmd_cancel_schedule))
    app.add_handler(SuffixCommandHandler("run_schedule", cmd_run_schedule))
    app.add_handler(CallbackQueryHandler(handle_schedule_callback, pattern=r"^sched_"))
    app.add_handler(CommandHandler("device", cmd_device))
    app.add_handler(CommandHandler("stats", cmd_device))
    app.add_handler(CommandHandler("battery", cmd_device))
    app.add_handler(CommandHandler("terminal", cmd_terminal))
    app.add_handler(CommandHandler("interrupt", cmd_interrupt))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_new))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(ResumeCommandHandler(cmd_resume))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("reiniciar", cmd_restart))
    app.add_handler(CommandHandler("shutdown", cmd_shutdown))
    app.add_handler(CommandHandler("desligar", cmd_shutdown))
    app.add_handler(CommandHandler("stop", cmd_shutdown))

    # General text and media messages (photos, voice notes, audio, documents)
    media_filters = (
        filters.TEXT
        | filters.PHOTO
        | filters.VOICE
        | filters.AUDIO
        | filters.Document.ALL
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(media_filters, handle_message))

    # Fallback for commands starting with / (e.g. /bin/ls in terminal mode or unknown commands)
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    # Error handling
    app.add_error_handler(error_handler)

    return app


__all__ = [
    "create_application",
    "post_init",
    "post_shutdown",
    "error_handler",
    "cleanup_old_media",
    "download_telegram_file_with_retry",
    "get_session_from_update",
    "cmd_start",
    "cmd_help",
    "cmd_status",
    "cmd_usage",
    "cmd_id",
    "cmd_new",
    "cmd_cancel",
    "cmd_interrupt",
    "cmd_device",
    "cmd_stats",
    "cmd_cd",
    "cmd_autopilot",
    "cmd_mode",
    "cmd_recent",
    "cmd_resume",
    "cmd_restart",
    "cmd_shutdown",
    "handle_unknown_command",
    "SuffixCommandHandler",
    "ResumeCommandHandler",
    "BOT_COMMANDS",
    "cmd_schedules",
    "cmd_schedule",
    "cmd_cancel_schedule",
    "cmd_run_schedule",
    "handle_schedule_callback",
    "build_prompt_with_context",
    "prepare_prompt_from_message",
    "prepare_prompt_from_messages",
    "cmd_terminal",
    "execute_terminal_command",
    "handle_message",
    "schedule_store",
    "scheduler_service",
    "format_timestamp",
    "compute_next_run",
    "parse_relative_time",
    "TELEGRAM_TOKEN",
    "AGY_BIN_PATH",
    "INJECT_TELEGRAM_CONTEXT",
    "MEDIA_DIR",
    "load_authorized_users",
    "get_usage",
]
