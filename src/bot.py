"""Telegram bot handlers and application setup."""

import asyncio
import logging
from pathlib import Path
from telegram import Update, BotCommand, BotCommandScopeChat, MessageEntity
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .config import TELEGRAM_TOKEN, AGY_BIN_PATH, INJECT_TELEGRAM_CONTEXT, MEDIA_DIR
from .auth import restricted, load_authorized_users
from .session_manager import session_manager
from .formatters import split_message, safe_reply, RateLimitedMessageUpdater

logger = logging.getLogger(__name__)


def build_prompt_with_context(prompt_text: str, user=None) -> str:
    """Optionally inject context so the CLI agent knows the prompt is coming from Telegram."""
    if not INJECT_TELEGRAM_CONTEXT:
        return prompt_text

    user_identifier = f"@{user.username}" if (user and getattr(user, "username", None)) else (getattr(user, "first_name", None) or "User")
    context_prefix = (
        f"[Context: Prompt sent by {user_identifier} via Telegram bot. "
        f"You are responding through Telegram chat. "
        f"Keep responses clear, concise, and mobile-friendly where appropriate. "
        f"Avoid expecting interactive terminal input.]\n\n"
    )
    return f"{context_prefix}{prompt_text}"


async def prepare_prompt_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    """
    Extract prompt text and handle media downloads (photos, voice notes, audio, documents).
    Returns (prompt_text, initial_status_description) or ("", "") if unsupported.
    """
    msg = update.effective_message
    if not msg:
        return "", ""

    # 1. Voice Message (.ogg Opus)
    if msg.voice:
        file_id = msg.voice.file_id
        file_obj = await context.bot.get_file(file_id)
        filename = f"voice_{msg.message_id}_{file_id[:8]}.ogg"
        local_path = MEDIA_DIR / filename
        await file_obj.download_to_drive(str(local_path))
        caption = msg.caption.strip() if msg.caption else ""
        caption_part = f"\nUser's note: {caption}" if caption else ""
        prompt_text = (
            f"[Attached Voice Note: {local_path} (Use view_file to inspect/listen)]\n\n"
            f"The user sent a voice message. Please listen to or inspect this audio file and respond to the user's spoken request.{caption_part}"
        )
        return prompt_text, "🎙️ Processing voice message..."

    # 2. Photo
    if msg.photo:
        photo = msg.photo[-1]  # Highest resolution
        file_id = photo.file_id
        file_obj = await context.bot.get_file(file_id)
        filename = f"photo_{msg.message_id}_{file_id[:8]}.jpg"
        local_path = MEDIA_DIR / filename
        await file_obj.download_to_drive(str(local_path))
        caption = msg.caption.strip() if msg.caption else ""
        user_request = caption if caption else "Please inspect the attached image and assist me with it."
        prompt_text = (
            f"[Attached Image: {local_path} (Use view_file to inspect)]\n\n"
            f"{user_request}"
        )
        return prompt_text, "🖼️ Processing image..."

    # 3. Audio file (MP3, WAV, etc.)
    if msg.audio:
        file_id = msg.audio.file_id
        file_obj = await context.bot.get_file(file_id)
        orig_ext = Path(msg.audio.file_name).suffix if msg.audio.file_name else ".mp3"
        filename = f"audio_{msg.message_id}_{file_id[:8]}{orig_ext}"
        local_path = MEDIA_DIR / filename
        await file_obj.download_to_drive(str(local_path))
        caption = msg.caption.strip() if msg.caption else ""
        caption_part = f"\nUser's note: {caption}" if caption else ""
        prompt_text = (
            f"[Attached Audio File: {local_path} (Use view_file to inspect/listen)]\n\n"
            f"Please listen to or analyze this audio file and assist me with it.{caption_part}"
        )
        return prompt_text, "🎵 Processing audio file..."

    # 4. Document (e.g. image or other file sent uncompressed)
    if msg.document:
        file_id = msg.document.file_id
        file_obj = await context.bot.get_file(file_id)
        orig_name = msg.document.file_name or "file"
        safe_name = "".join(c for c in orig_name if c.isalnum() or c in "._- ")
        filename = f"doc_{msg.message_id}_{safe_name}"
        local_path = MEDIA_DIR / filename
        await file_obj.download_to_drive(str(local_path))
        caption = msg.caption.strip() if msg.caption else ""
        user_request = caption if caption else f"Please inspect the attached file ({orig_name})."
        prompt_text = (
            f"[Attached Document: {local_path} (Use view_file to inspect)]\n\n"
            f"{user_request}"
        )
        return prompt_text, f"📄 Processing document ({orig_name})..."

    # 5. Plain text
    if msg.text:
        return msg.text.strip(), "🤖 *Antigravity:* Initializing..."

    return "", ""


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome and overview message."""
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return

    try:
        await context.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=update.effective_chat.id))
    except Exception:
        pass

    text = (
        f"👋 Hello {user.first_name if user else 'there'}!\n\n"
        "I am your **Antigravity CLI** bridge on this computer.\n"
        "Send me any message, photo, voice note, or prompt, and I will forward it to `agy`.\n\n"
        "🛠 **Available Commands:**\n"
        "• `/status` - Check current session, directory & settings\n"
        "• `/new` - Start a fresh conversation turn\n"
        "• `/recent` - View recent CLI conversations to resume\n"
        "• `/resume <id|number>` - Resume a past conversation (e.g. `/resume_1`)\n"
        "• `/cd <path>` - Change working directory\n"
        "• `/autopilot <on|off>` - Toggle tool auto-approvals\n"
        "• `/mode <plan|accept-edits|clear>` - Set agent execution mode\n"
        "• `/cancel` - Stop / cancel currently running turn\n"
        "• `/help` - Show this help message"
    )
    await safe_reply(msg, text)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed help message."""
    await cmd_start(update, context)


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display active session status and environment."""
    msg = update.effective_message
    if not msg:
        return

    user = update.effective_user
    session = session_manager.get_session(update.effective_chat.id)
    cid = session.conversation_id or "*(None - will create new)*"
    status_emoji = "🟢 Running" if session.runner.is_running else "⚪ Idle"
    autopilot_str = "Enabled (`--dangerously-skip-permissions`)" if session.autopilot else "Disabled (safe/ask)"

    text = (
        "📊 **Antigravity Session Status**\n\n"
        f"• **Status:** {status_emoji}\n"
        f"• **Telegram User ID:** `{user.id if user else 'unknown'}`\n"
        f"• **Conversation ID:** `{cid}`\n"
        f"• **Working Directory:** `{session.cwd}`\n"
        f"• **Autopilot:** {autopilot_str}\n"
        f"• **Mode:** `{session.mode or 'default'}`\n"
        f"• **CLI Binary:** `{AGY_BIN_PATH}`"
    )
    await safe_reply(msg, text)


@restricted
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the sender's numeric Telegram User ID."""
    user = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    user_id = user.id if user else "unknown"
    await safe_reply(msg, f"🆔 Your numeric Telegram User ID is:\n`{user_id}`")


@restricted
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new conversation."""
    msg = update.effective_message
    if not msg:
        return

    session = session_manager.get_session(update.effective_chat.id)
    if session.runner.is_running:
        await safe_reply(msg, "⚠️ A turn is currently running. Send /cancel first.")
        return

    session.reset_conversation()
    await safe_reply(msg, "✨ New conversation started! Your next prompt will begin a fresh session.")


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel currently executing command/turn."""
    msg = update.effective_message
    if not msg:
        return

    session = session_manager.get_session(update.effective_chat.id)
    if not session.runner.is_running:
        await safe_reply(msg, "⚪ No task is currently running.")
        return

    success = await session.runner.cancel()
    if success:
        await safe_reply(msg, "⏹️ Sent cancellation request to active agent process.")
    else:
        await safe_reply(msg, "⚠️ Could not cancel active process.")


@restricted
async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change the working directory."""
    msg = update.effective_message
    if not msg:
        return

    if not context.args:
        await safe_reply(msg, "Usage: `/cd <directory_path>`")
        return

    raw_path = " ".join(context.args)
    target = Path(raw_path).expanduser().resolve()

    if not target.exists() or not target.is_dir():
        await safe_reply(msg, f"❌ Directory does not exist:\n`{target}`")
        return

    session = session_manager.get_session(update.effective_chat.id)
    session.cwd = str(target)
    await safe_reply(msg, f"📁 Working directory changed to:\n`{session.cwd}`")


@restricted
async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle tool auto-approval (--dangerously-skip-permissions)."""
    msg = update.effective_message
    if not msg:
        return

    session = session_manager.get_session(update.effective_chat.id)
    if not context.args:
        curr = "ON" if session.autopilot else "OFF"
        await safe_reply(msg, f"Autopilot is currently **{curr}**.\nUse `/autopilot on` or `/autopilot off`.")
        return

    arg = context.args[0].lower()
    if arg in ("on", "true", "1", "enable"):
        session.autopilot = True
        await safe_reply(msg, "🚀 Autopilot turned **ON** (`--dangerously-skip-permissions` enabled).")
    elif arg in ("off", "false", "0", "disable"):
        session.autopilot = False
        await safe_reply(msg, "🛡️ Autopilot turned **OFF**.")
    else:
        await safe_reply(msg, "Usage: `/autopilot <on|off>`")


@restricted
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set execution mode: plan, accept-edits, or clear."""
    msg = update.effective_message
    if not msg:
        return

    session = session_manager.get_session(update.effective_chat.id)
    if not context.args:
        await safe_reply(msg, f"Current mode: `{session.mode or 'default'}`\nUsage: `/mode <plan|accept-edits|clear>`")
        return

    mode_arg = context.args[0].lower()
    if mode_arg in ("clear", "none", "reset"):
        session.mode = None
        await safe_reply(msg, "🔄 Mode reset to default.")
    elif mode_arg in ("plan", "accept-edits"):
        session.mode = mode_arg
        await safe_reply(msg, f"⚙️ Mode set to: `{session.mode}`")
    else:
        await safe_reply(msg, "Invalid mode. Choose `plan`, `accept-edits`, or `clear`.")


@restricted
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent Antigravity CLI conversations."""
    msg = update.effective_message
    if not msg:
        return

    convs = session_manager.list_recent_conversations(limit=7)
    if not convs:
        await safe_reply(msg, "No recent conversations found in history.")
        return

    session = session_manager.get_session(update.effective_chat.id)
    session.recent_conversations = [c["id"] for c in convs]

    lines = ["🕒 **Recent Conversations:**\n"]
    for i, c in enumerate(convs, 1):
        cid = c["id"]
        preview = c["preview"]
        time_str = c.get("time_str", "")
        is_running = c.get("is_running", False) or session_manager.is_conversation_running(cid)
        status_tag = " 🟢 *[Running]*" if is_running else ""
        lines.append(f"{i}. `{cid[:8]}...` ({time_str}){status_tag}\n   _{preview}_\n   👉 /resume_{i}\n")

    await safe_reply(msg, "\n".join(lines))


@restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume a specific conversation ID or recent conversation index."""
    msg = update.effective_message
    if not msg:
        return

    if not context.args:
        # Listing chats when /resume is called without an argument
        await cmd_recent(update, context)
        return

    raw_arg = context.args[0].strip()
    session = session_manager.get_session(update.effective_chat.id)
    if session.runner.is_running:
        await safe_reply(msg, "⚠️ A turn is currently running. Please cancel or wait.")
        return

    cid = None
    # Check if user provided an index (e.g. /resume_1 or /resume 1)
    if raw_arg.isdigit():
        idx = int(raw_arg)
        recent_list = session.recent_conversations
        if not recent_list:
            convs = session_manager.list_recent_conversations(limit=7)
            recent_list = [c["id"] for c in convs]
            session.recent_conversations = recent_list

        if 1 <= idx <= len(recent_list):
            cid = recent_list[idx - 1]
        else:
            await safe_reply(
                msg,
                f"⚠️ Invalid conversation number `{idx}`. Please choose 1 to {max(1, len(recent_list))} from /recent.",
            )
            return
    else:
        # Check if underscores were used in place of hyphens in a UUID (e.g. /resume_<uuid_with_underscores>)
        clean_arg = raw_arg
        if "_" in clean_arg and "-" not in clean_arg:
            clean_arg = clean_arg.replace("_", "-")

        # Check for prefix match in recent conversations if short ID was passed
        if len(clean_arg) >= 6 and len(clean_arg) < 36:
            convs = session_manager.list_recent_conversations(limit=20)
            for c in convs:
                if c["id"].lower().startswith(clean_arg.lower()):
                    cid = c["id"]
                    break

        if not cid:
            cid = clean_arg

    session.conversation_id = cid
    running_tag = "\n⚡ *Note:* This conversation is currently active/running." if session_manager.is_conversation_running(cid) else ""
    await safe_reply(msg, f"🔄 Resumed conversation:\n`{cid}`{running_tag}\nYour next prompt will continue this session.")


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle normal prompt messages and media (photos, voice notes, audio, documents)."""
    msg = update.effective_message
    if not msg:
        return

    session = session_manager.get_session(update.effective_chat.id)

    if session.runner.is_running:
        await safe_reply(msg, "⏳ An agent turn is already in progress. Send /cancel to interrupt it.")
        return

    # Extract prompt text and handle media downloads
    try:
        prompt_text, init_status = await prepare_prompt_from_message(update, context)
    except Exception as e:
        logger.error("Failed to process message media: %s", e, exc_info=True)
        await safe_reply(msg, f"❌ Failed to process attached file: {e}")
        return

    if not prompt_text:
        return

    # Post initial status message
    status_msg = await safe_reply(msg, init_status)
    if not status_msg:
        return

    updater = RateLimitedMessageUpdater(status_msg, min_interval=1.5)
    streaming_text: list[str] = []
    current_tool: str = ""

    async def on_event(event_type: str, data: any) -> None:
        nonlocal current_tool
        if event_type == "tool_start":
            tname = data.get("name", "tool")
            current_tool = tname
            await updater.update(f"⚙️ Running tool: `{tname}`...")
        elif event_type == "tool_done":
            current_tool = ""
            await updater.update("🤖 Processing results...")
        elif event_type == "thought":
            pass
        elif event_type == "text_delta":
            streaming_text.append(data)
            accumulated = "".join(streaming_text)
            if len(accumulated) > 10 and not current_tool:
                # Preview partial text (up to last 1500 chars)
                preview = accumulated if len(accumulated) < 1500 else "..." + accumulated[-1500:]
                await updater.update(f"💬 {preview}")

    # Run the prompt with injected Telegram context if enabled
    full_prompt = build_prompt_with_context(prompt_text, user=update.effective_user)
    result = await session.runner.run(
        prompt=full_prompt,
        conversation_id=session.conversation_id,
        cwd=session.cwd,
        autopilot=session.autopilot,
        mode=session.mode,
        model=session.model,
        effort=session.effort,
        event_callback=on_event,
    )

    # Update session conversation ID
    if result.get("conversation_id"):
        session.conversation_id = result["conversation_id"]

    status = result.get("status")
    response_text = result.get("response", "").strip()
    error_msg = result.get("error", "")

    if status == "CANCELLED":
        await updater.update("⏹️ Execution was cancelled.", force=True)
        return

    if status == "ERROR":
        err_display = f"❌ **Error executing turn:**\n```\n{error_msg}\n```"
        await updater.update(err_display, force=True)
        return

    if not response_text:
        await updater.update("⚠️ Turn completed with empty output.", force=True)
        return

    # Response is available: chunk it and send
    chunks = split_message(response_text)

    # Edit initial message with the first chunk
    edited = await updater.update(chunks[0], force=True)
    if not edited:
        # If edit failed (e.g. formatting or rate limit), send clean reply
        await safe_reply(msg, chunks[0])

    # Send any remaining chunks as separate messages
    for chunk in chunks[1:]:
        await safe_reply(msg, chunk)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)


class ResumeCommandHandler(CommandHandler):
    """
    Handles /resume, /resume <arg>, and clickable /resume_<arg> commands (e.g. /resume_1).
    """

    def __init__(self, callback):
        super().__init__("resume", callback)

    def check_update(self, update: object):
        if isinstance(update, Update) and update.effective_message:
            message = update.effective_message
            if (
                message.entities
                and message.entities[0].type == MessageEntity.BOT_COMMAND
                and message.entities[0].offset == 0
                and message.text
            ):
                command = message.text[1 : message.entities[0].length]
                args = message.text.split()[1:]
                command_parts = command.split("@")

                try:
                    bot = message.get_bot()
                    bot_username = bot.username if bot else None
                except Exception:
                    bot_username = None

                if bot_username:
                    command_parts.append(bot_username)

                base_cmd = command_parts[0].lower()
                if not (base_cmd == "resume" or base_cmd.startswith("resume_")):
                    return None

                if bot_username and len(command_parts) > 2 and command_parts[1].lower() != bot_username.lower():
                    return None

                if base_cmd.startswith("resume_") and not args:
                    extracted = base_cmd[len("resume_") :]
                    args = [extracted]

                if not self._check_correct_args(args):
                    return None

                filter_result = self.filters.check_update(update) if self.filters else True
                if filter_result:
                    return args, filter_result
                return False
        return None


BOT_COMMANDS = [
    BotCommand("status", "Show session status & working directory"),
    BotCommand("new", "Start a fresh conversation turn"),
    BotCommand("recent", "View recent CLI conversations"),
    BotCommand("resume", "Resume a previous conversation (ID or number)"),
    BotCommand("cancel", "Cancel currently running turn"),
    BotCommand("cd", "Change working directory"),
    BotCommand("autopilot", "Toggle tool auto-approval (on/off)"),
    BotCommand("mode", "Set execution mode (plan/accept-edits/clear)"),
    BotCommand("id", "Show numeric Telegram User ID"),
    BotCommand("help", "Show help and command guide"),
]


async def post_init(app: Application) -> None:
    """Register command autosuggestions scoped strictly to authorized users."""
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


def create_application() -> Application:
    """Build and configure the Telegram application."""
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is not set in environment or .env file.")

    load_authorized_users()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_new))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(ResumeCommandHandler(cmd_resume))

    # General text and media messages (photos, voice notes, audio, documents)
    media_filters = (
        filters.TEXT
        | filters.PHOTO
        | filters.VOICE
        | filters.AUDIO
        | filters.Document.ALL
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(media_filters, handle_message))

    # Error handling
    app.add_error_handler(error_handler)

    return app
