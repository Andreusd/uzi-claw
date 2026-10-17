"""Administrative, session, and utility command handlers for UziClaw."""

import functools
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from telegram import Update, BotCommand, BotCommandScopeChat, MessageEntity
from telegram.ext import CommandHandler, ContextTypes

from ..config import AGY_BIN_PATH, FORUM_GROUP_ID
from ..auth import restricted
from ..session_manager import session_manager, SessionState
from ..formatters import safe_reply, safe_send, safe_edit, format_usage_data
from ..antigravity_runner import get_usage
from ..device_stats import format_device_stats
from ..scheduler import scheduler_service
from .forum_helper import (
    is_forum_general_chat,
    FORUM_TOPIC_ICONS,
    create_forum_topic_with_retry,
    unpin_topic_messages_quietly,
    generate_topic_name,
    get_topic_icon_id,
)

logger = logging.getLogger(__name__)


def general_topic_redirect(func):
    """
    Ensure commands executed in the General topic of a forum supergroup create
    a new dedicated forum topic and reply there, never replying in the General topic.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        msg = update.effective_message
        if is_forum_general_chat(chat, msg):
            cmd_name = func.__name__.replace("cmd_", "")
            now_str = datetime.now().strftime("%d/%m %H:%M")
            topic_name = f"🛠️ /{cmd_name} {now_str}"
            topic = await create_forum_topic_with_retry(
                bot=context.bot,
                chat_id=chat.id,
                name=topic_name,
                icon_custom_emoji_id=FORUM_TOPIC_ICONS["command"],
            )
            target_thread = getattr(topic, "message_thread_id", None)
            if target_thread is None:
                logger.error("Failed to create topic for /%s in General after retries", cmd_name)
                return

            orig_reply = msg.reply_text
            async def redirected_reply(*r_args, **r_kwargs):
                r_kwargs["message_thread_id"] = target_thread
                sent = await context.bot.send_message(chat_id=chat.id, *r_args, **r_kwargs)
                await unpin_topic_messages_quietly(context.bot, chat.id, target_thread)
                return sent
            msg.reply_text = redirected_reply
            try:
                return await func(update, context, *args, **kwargs)
            finally:
                msg.reply_text = orig_reply

        return await func(update, context, *args, **kwargs)
    return wrapper


def get_session_from_update(update: Update) -> SessionState:
    """Retrieve SessionState taking effective_chat.id and message_thread_id into account."""
    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    msg = update.effective_message
    raw_thread_id = getattr(msg, "message_thread_id", None) if msg else None
    thread_id = raw_thread_id if isinstance(raw_thread_id, int) else None
    return session_manager.get_session(chat_id, thread_id)


BOT_COMMANDS = [
    BotCommand("status", "Show session status & working directory"),
    BotCommand("schedules", "View & manage scheduled tasks (no LLM)"),
    BotCommand("schedule", "Directly create a scheduled task"),
    BotCommand("device", "Check device stats (battery, CPU, RAM, disk)"),
    BotCommand("terminal", "Toggle direct terminal mode (no LLM)"),
    BotCommand("interrupt", "Interrupt current prompt (Esc in agy CLI)"),
    BotCommand("cancel", "Cancel currently running turn"),
    BotCommand("usage", "Show model quotas & limits remaining"),
    BotCommand("new", "Start a fresh conversation turn"),
    BotCommand("recent", "View recent CLI conversations"),
    BotCommand("resume", "Resume a previous conversation (ID or number)"),
    BotCommand("cd", "Change working directory"),
    BotCommand("autopilot", "Toggle tool auto-approval (on/off)"),
    BotCommand("mode", "Set execution mode (plan/accept-edits/clear)"),
    BotCommand("id", "Show numeric Telegram User ID"),
    BotCommand("help", "Show help and command guide"),
]


@restricted
@general_topic_redirect
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
        "I am **UziClaw**, your AI agent bridge on this computer.\n"
        "Send me any message, photo, voice note, or prompt, and I will forward it to `agy`.\n\n"
        "🛠 **Available Commands:**\n"
        "• `/status` - Check current session, directory & settings\n"
        "• `/schedules` - View and manage scheduled tasks (no LLM)\n"
        "• `/device` - Device stats & battery level (or `/battery`)\n"
        "• `/terminal` - Toggle direct terminal mode (shell commands, no LLM)\n"
        "• `/interrupt` - Interrupt active prompt (Esc in agy CLI)\n"
        "• `/cancel` - Stop / cancel currently running turn\n"
        "• `/usage` - View model quotas & limits remaining\n"
        "• `/new` - Start a fresh conversation turn\n"
        "• `/recent` - View recent CLI conversations to resume\n"
        "• `/resume <id|number>` - Resume a past conversation (e.g. `/resume_1`)\n"
        "• `/cd <path>` - Change working directory\n"
        "• `/autopilot <on|off>` - Toggle tool auto-approvals\n"
        "• `/mode <plan|accept-edits|clear>` - Set agent execution mode\n"
        "• `/help` - Show this help message"
    )
    await safe_reply(msg, text)


@restricted
@general_topic_redirect
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed help message."""
    await cmd_start(update, context)


@restricted
@general_topic_redirect
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display active session status and environment."""
    msg = update.effective_message
    if not msg:
        return

    user = update.effective_user
    session = get_session_from_update(update)
    cid = session.conversation_id or "*(None - will create new)*"

    if session.terminal_runner.is_running:
        status_emoji = "🟢 Running (terminal)"
    elif session.runner.is_running or session.is_busy:
        status_emoji = "🟢 Running (agent)"
    else:
        status_emoji = "⚪ Idle"

    if session.queued_count > 0:
        status_emoji += f" ({session.queued_count} queued)"

    autopilot_str = "Enabled (`--dangerously-skip-permissions`)" if session.autopilot else "Disabled (safe/ask)"
    terminal_str = "🟢 Active (shell)" if session.terminal_mode else "⚪ Inactive (LLM mode)"

    text = (
        "📊 **UziClaw Session Status**\n\n"
        f"• **Status:** {status_emoji}\n"
        f"• **Telegram User ID:** `{user.id if user else 'unknown'}`\n"
        f"• **Conversation ID:** `{cid}`\n"
        f"• **Working Directory:** `{session.cwd}`\n"
        f"• **Terminal Mode:** {terminal_str}\n"
        f"• **Autopilot:** {autopilot_str}\n"
        f"• **Mode:** `{session.mode or 'default'}`\n"
        f"• **CLI Binary:** `{AGY_BIN_PATH}`"
    )
    await safe_reply(msg, text)


def _get_usage_func():
    import sys
    bot_mod = sys.modules.get("src.bot")
    if bot_mod and hasattr(bot_mod, "get_usage"):
        return getattr(bot_mod, "get_usage")
    return get_usage


@restricted
@general_topic_redirect
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show quota and usage limits equivalent to agy /usage."""
    msg = update.effective_message
    if not msg:
        return

    status_msg = await safe_reply(msg, "⏳ Fetching quota usage from `agy`...")
    try:
        usage_fn = _get_usage_func()
        usage_data, raw_text = await usage_fn()
        if usage_data:
            formatted = format_usage_data(usage_data)
        elif raw_text:
            formatted = f"📊 *UziClaw Quota Usage*\n\n```\n{raw_text}\n```"
        else:
            formatted = "❌ Could not retrieve usage quota from `agy`. Please try again."

        if status_msg:
            edited = await safe_edit(status_msg, formatted)
            if not edited:
                await safe_reply(msg, formatted)
        else:
            await safe_reply(msg, formatted)
    except Exception as e:
        logger.error("Error in cmd_usage: %s", e, exc_info=True)
        err_msg = f"❌ Error retrieving usage: `{e}`"
        if status_msg:
            edited = await safe_edit(status_msg, err_msg)
            if not edited:
                await safe_reply(msg, err_msg)
        else:
            await safe_reply(msg, err_msg)


@restricted
@general_topic_redirect
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
    """Start a new conversation turn with topic isolation."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    is_general = is_forum_general_chat(chat, msg)
    is_private = (chat.type == "private")
    now_str = datetime.now().strftime("%d/%m %H:%M")

    # 1. In General topic of a forum supergroup:
    if is_general:
        topic_name = f"💬 Nova Conversa {now_str}"
        topic = await create_forum_topic_with_retry(
            bot=context.bot,
            chat_id=chat.id,
            name=topic_name,
            icon_custom_emoji_id=FORUM_TOPIC_ICONS["chat"],
        )
        target_thread_id = getattr(topic, "message_thread_id", None)
        if target_thread_id is None:
            logger.error("Failed to create topic for /new in General after retries")
            return

        session = session_manager.get_session(chat.id, target_thread_id)
        session.reset_conversation()
        session_manager.save_session(session)

        welcome_text = (
            "✨ **Nova conversa iniciada!**\n"
            "Envie sua mensagem ou prompt diretamente aqui neste tópico."
        )
        await safe_send(context.bot, chat.id, welcome_text, thread_id=target_thread_id)
        await unpin_topic_messages_quietly(context.bot, chat.id, target_thread_id)
        return

    # 2. In Private DM:
    if is_private:
        session = get_session_from_update(update)
        if session.terminal_runner.is_running:
            await safe_reply(msg, "⚠️ A terminal command is currently running. Please /cancel it first.")
            return

        is_running_turn = session.runner.is_running or session.is_busy
        if is_running_turn:
            session.detach_running_turn()
        else:
            session.reset_conversation()

        topic = None
        if FORUM_GROUP_ID and context and getattr(context, "bot", None):
            topic_name = f"💬 Nova Conversa {now_str}"
            topic = await create_forum_topic_with_retry(
                bot=context.bot,
                chat_id=FORUM_GROUP_ID,
                name=topic_name,
                icon_custom_emoji_id=FORUM_TOPIC_ICONS["chat"],
            )
            if topic and hasattr(topic, "message_thread_id"):
                session.forum_topic_id = topic.message_thread_id

        session_manager.save_session(session)

        if is_running_turn:
            reply_text = (
                "✨ **Nova conversa iniciada!**\n"
                "O processamento anterior continua executando em segundo plano e entregará a resposta assim que terminar.\n"
                "Sua próxima mensagem já começará em um contexto novo e independente."
            )
        else:
            topic_mention = f"\nTópico dedicado criado no grupo: **{topic.name}**" if topic else ""
            reply_text = f"✨ **Nova conversa iniciada!**{topic_mention}\nSua próxima mensagem começará em um contexto novo."

        await safe_reply(msg, reply_text)

        if topic and FORUM_GROUP_ID:
            await safe_send(
                context.bot,
                FORUM_GROUP_ID,
                "✨ **Nova conversa iniciada via chat privado.** Envie suas mensagens no privado ou aqui neste tópico.",
                thread_id=topic.message_thread_id,
            )
            await unpin_topic_messages_quietly(context.bot, FORUM_GROUP_ID, topic.message_thread_id)
        return

    # 3. Inside an existing forum topic (not General):
    session = get_session_from_update(update)
    if session.terminal_runner.is_running:
        await safe_reply(msg, "⚠️ A terminal command is currently running. Please /cancel it first.")
        return

    topic_name = f"💬 Nova Conversa {now_str}"
    topic = await create_forum_topic_with_retry(
        bot=context.bot,
        chat_id=chat.id,
        name=topic_name,
        icon_custom_emoji_id=FORUM_TOPIC_ICONS["chat"],
    )
    new_thread_id = getattr(topic, "message_thread_id", None)
    if new_thread_id is None:
        logger.error("Failed to create topic in forum group for /new after retries")
        session.reset_conversation()
        session_manager.save_session(session)
        await safe_reply(msg, "✨ Nova conversa iniciada!")
        return

    new_session = session_manager.get_session(chat.id, new_thread_id)
    new_session.reset_conversation()
    session_manager.save_session(new_session)

    await safe_reply(msg, f"✨ Nova conversa iniciada no tópico **{topic.name}**.")
    await safe_send(
        context.bot,
        chat.id,
        "✨ **Nova conversa iniciada!** Envie sua mensagem ou prompt diretamente aqui neste tópico.",
        thread_id=new_thread_id,
    )


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel currently executing command/turn."""
    msg = update.effective_message
    if not msg:
        return

    session = get_session_from_update(update)
    chat_id = update.effective_chat.id if update.effective_chat else None
    raw_thread_id = getattr(msg, "message_thread_id", None) if msg else None

    had_queued = session.queued_count > 0
    if had_queued:
        session.clear_queue()

    cancelled_any = False
    if session.terminal_runner.is_running:
        success = await session.terminal_runner.cancel()
        if success:
            extra = " and cleared queued commands." if had_queued else "."
            await safe_reply(msg, f"⏹️ Sent cancellation (SIGTERM) to running terminal command{extra}")
            return

    if session.runner.is_running:
        success = await session.runner.cancel()
        if success:
            cancelled_any = True

    for bg_runner in list(session.background_runners):
        if bg_runner.is_running:
            await bg_runner.cancel()
            cancelled_any = True
    session.background_runners.clear()

    # Also check and cancel active scheduled tasks for this chat / thread
    if chat_id:
        cancelled_scheds = await scheduler_service.cancel_active_jobs_for_chat(chat_id, raw_thread_id)
        if cancelled_scheds:
            cancelled_any = True

    if cancelled_any:
        extra = " and cleared queued messages." if had_queued else "."
        await safe_reply(msg, f"⏹️ Sent cancellation request to active agent process{extra}")
        return

    if session.is_busy:
        session.cancel_requested = True
        extra = " and cleared queued messages." if had_queued else "."
        await safe_reply(msg, f"⏹️ Cancelled pending turn before startup{extra}")
        return

    if had_queued:
        await safe_reply(msg, "⏹️ Cleared queued messages.")
        return

    await safe_reply(msg, "⚪ No task is currently running.")


@restricted
async def cmd_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interrupt currently executing prompt (equivalent to pressing Esc in agy CLI / sending SIGINT)."""
    msg = update.effective_message
    if not msg:
        return

    session = get_session_from_update(update)
    chat_id = update.effective_chat.id if update.effective_chat else None
    raw_thread_id = getattr(msg, "message_thread_id", None) if msg else None

    if session.terminal_runner.is_running:
        success = await session.terminal_runner.interrupt()
        if success:
            await safe_reply(msg, "⏹️ Sent interrupt (Ctrl+C / SIGINT) to running terminal command.")
            return

    if session.runner.is_running:
        success = await session.runner.interrupt()
        if success:
            await safe_reply(msg, "⏹️ Interrupted active prompt (Esc / SIGINT sent to `agy`).")
            return

    for bg_runner in list(session.background_runners):
        if bg_runner.is_running:
            await bg_runner.interrupt()
            await safe_reply(msg, "⏹️ Interrupted active prompt (Esc / SIGINT sent to `agy`).")
            return

    if chat_id:
        cancelled_scheds = await scheduler_service.cancel_active_jobs_for_chat(chat_id, raw_thread_id)
        if cancelled_scheds:
            await safe_reply(msg, f"⏹️ Interrupted active scheduled task(s) (#{', #'.join(map(str, cancelled_scheds))}).")
            return

    if session.is_busy:
        session.cancel_requested = True
        await safe_reply(msg, "⏹️ Interrupted pending prompt before startup.")
        return

    await safe_reply(msg, "⚪ No task or prompt is currently running.")


@restricted
@general_topic_redirect
async def cmd_device(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display host device statistics including battery, CPU, RAM, disk, and uptime."""
    msg = update.effective_message
    if not msg:
        return

    stats_text = format_device_stats()
    await safe_reply(msg, stats_text)


# Backward-compatibility alias
cmd_stats = cmd_device


@restricted
@general_topic_redirect
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

    session = get_session_from_update(update)
    session.cwd = str(target)
    session_manager.save_session(session)
    await safe_reply(msg, f"📁 Working directory changed to:\n`{session.cwd}`")


@restricted
@general_topic_redirect
async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle tool auto-approval (--dangerously-skip-permissions)."""
    msg = update.effective_message
    if not msg:
        return

    session = get_session_from_update(update)
    if not context.args:
        curr = "ON" if session.autopilot else "OFF"
        await safe_reply(msg, f"Autopilot is currently **{curr}**.\nUse `/autopilot on` or `/autopilot off`.")
        return

    arg = context.args[0].lower()
    if arg in ("on", "true", "1", "enable"):
        session.autopilot = True
        session_manager.save_session(session)
        await safe_reply(msg, "🚀 Autopilot turned **ON** (`--dangerously-skip-permissions` enabled).")
    elif arg in ("off", "false", "0", "disable"):
        session.autopilot = False
        session_manager.save_session(session)
        await safe_reply(msg, "🛡️ Autopilot turned **OFF**.")
    else:
        await safe_reply(msg, "Usage: `/autopilot <on|off>`")


@restricted
@general_topic_redirect
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set execution mode: plan, accept-edits, or clear."""
    msg = update.effective_message
    if not msg:
        return

    session = get_session_from_update(update)
    if not context.args:
        await safe_reply(msg, f"Current mode: `{session.mode or 'default'}`\nUsage: `/mode <plan|accept-edits|clear>`")
        return

    mode_arg = context.args[0].lower()
    if mode_arg in ("clear", "none", "reset"):
        session.mode = None
        session_manager.save_session(session)
        await safe_reply(msg, "🔄 Mode reset to default.")
    elif mode_arg in ("plan", "accept-edits"):
        session.mode = mode_arg
        session_manager.save_session(session)
        await safe_reply(msg, f"⚙️ Mode set to: `{session.mode}`")
    else:
        await safe_reply(msg, "Invalid mode. Choose `plan`, `accept-edits`, or `clear`.")


@restricted
@general_topic_redirect
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent Antigravity CLI conversations."""
    msg = update.effective_message
    if not msg:
        return

    convs = session_manager.list_recent_conversations(limit=7)
    if not convs:
        await safe_reply(msg, "No recent conversations found in history.")
        return

    session = get_session_from_update(update)
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
    """Resume a specific conversation ID or recent conversation index with history dump and topic reuse."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    is_general = is_forum_general_chat(chat, msg)
    is_private = (chat.type == "private")

    if not context.args:
        # Listing chats when /resume is called without an argument
        await cmd_recent(update, context)
        return

    raw_arg = context.args[0].strip()
    session = get_session_from_update(update)
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

    # Determine topic target and whether a new topic is needed
    raw_thread_id = getattr(msg, "message_thread_id", None)
    current_thread_id = raw_thread_id if isinstance(raw_thread_id, int) else None

    target_topic_id = None
    created_new_topic = False
    topic_name = ""

    # Case 1: Called inside an existing topic in the forum supergroup (not General)
    if not is_private and not is_general and current_thread_id and current_thread_id > 1:
        target_topic_id = current_thread_id
        session.conversation_id = cid
        session_manager.save_session(session)

    # Case 2: Called in Private DM or in General topic
    else:
        # Check if a forum topic ALREADY exists for this conversation
        existing_topic_id = session_manager.find_topic_for_conversation(cid)

        if existing_topic_id:
            # Topic already exists! DO NOT create a new topic.
            target_topic_id = existing_topic_id
            if is_private:
                session.conversation_id = cid
                session.forum_topic_id = existing_topic_id
                session_manager.save_session(session)
        else:
            # Topic does NOT exist yet. Only create one if FORUM_GROUP_ID is configured.
            if FORUM_GROUP_ID and context and getattr(context, "bot", None):
                first_prompt = session_manager.get_conversation_first_prompt(cid)
                topic_name = generate_topic_name(first_prompt or f"Conversa {cid[:8]}", default_prefix="PC CLI")
                icon_id = get_topic_icon_id(first_prompt)
                topic = await create_forum_topic_with_retry(
                    bot=context.bot,
                    chat_id=FORUM_GROUP_ID,
                    name=topic_name,
                    icon_custom_emoji_id=icon_id,
                )
                new_tid = getattr(topic, "message_thread_id", None)
                if new_tid:
                    target_topic_id = new_tid
                    created_new_topic = True
                    topic_session = session_manager.get_session(FORUM_GROUP_ID, new_tid)
                    topic_session.conversation_id = cid
                    session_manager.save_session(topic_session)
                    if is_private:
                        session.conversation_id = cid
                        session.forum_topic_id = new_tid
                        session_manager.save_session(session)
            elif is_private:
                session.conversation_id = cid
                session_manager.save_session(session)

    # Retrieve conversation turns for Option A history dump
    turns = session_manager.get_conversation_turns(cid, max_turns=2)
    running_tag = "\n⚡ *Nota:* Esta conversa está ativa/executando no PC." if session_manager.is_conversation_running(cid) else ""
    topic_mention = f"\n📌 *Tópico no grupo:* `{target_topic_id}`" if target_topic_id and is_private else ""
    confirm_text = f"🔄 **Conversa conectada com sucesso!**\n`{cid}`{running_tag}{topic_mention}\nSua próxima mensagem continuará este contexto."

    # Format history blocks
    history_messages = []
    if turns:
        history_header = f"📜 *Histórico recente da conversa retomada ({len(turns)} turno{'s' if len(turns) > 1 else ''}):*"
        history_messages.append(history_header)
        for i, turn in enumerate(turns, 1):
            u_text = turn["user"]
            if len(u_text) > 500:
                u_text = u_text[:497] + "..."
            a_text = turn["assistant"]
            if len(a_text) > 1400:
                a_text = a_text[:1397] + "...\n_(resposta truncada para exibição)_"
            tools_line = f"\n_🔧 Ferramentas: {', '.join(turn['tools'])}_" if turn.get("tools") else ""
            block = f"👤 **Você:**\n{u_text}\n\n🤖 **Antigravity:**\n{a_text}{tools_line}"
            history_messages.append(block)

    # Send messages based on origin
    if is_general:
        # In General topic: post history & confirmation to target topic, reply briefly in General
        if target_topic_id and FORUM_GROUP_ID:
            for h_msg in history_messages:
                await safe_send(context.bot, FORUM_GROUP_ID, h_msg, thread_id=target_topic_id)
            await safe_send(context.bot, FORUM_GROUP_ID, confirm_text, thread_id=target_topic_id)
            await unpin_topic_messages_quietly(context.bot, FORUM_GROUP_ID, target_topic_id)

            if created_new_topic:
                await safe_reply(msg, f"🔄 Conversa do PC conectada no novo tópico **{topic_name}**!\nEnvie suas mensagens por lá.")
            else:
                await safe_reply(msg, f"🔄 Conversa conectada no tópico dedicado existente (ID: `{target_topic_id}`)!\nEnvie suas mensagens por lá.")
        else:
            await safe_reply(msg, confirm_text)

    elif is_private:
        # In Private DM: send confirmation and history directly to user
        await safe_reply(msg, confirm_text)
        for h_msg in history_messages:
            await safe_reply(msg, h_msg)

        # Mirror/initialize topic in forum supergroup if newly created
        if FORUM_GROUP_ID and target_topic_id and created_new_topic:
            await safe_send(
                context.bot,
                FORUM_GROUP_ID,
                f"✨ **Conversa importada do PC CLI conectada via chat privado.**\n`{cid}`",
                thread_id=target_topic_id,
            )
            for h_msg in history_messages:
                await safe_send(context.bot, FORUM_GROUP_ID, h_msg, thread_id=target_topic_id)
            await unpin_topic_messages_quietly(context.bot, FORUM_GROUP_ID, target_topic_id)

    else:
        # Inside existing topic: reply right here
        await safe_reply(msg, confirm_text)
        for h_msg in history_messages:
            await safe_reply(msg, h_msg)
        if target_topic_id:
            await unpin_topic_messages_quietly(context.bot, chat.id, target_topic_id)


class SuffixCommandHandler(CommandHandler):
    """
    Handles /<base_cmd>, /<base_cmd> <arg>, and clickable /<base_cmd>_<arg> commands.
    """

    def __init__(self, command: str, callback):
        super().__init__(command, callback)
        self.base_name = command.lower()

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
                prefix = f"{self.base_name}_"
                if not (base_cmd == self.base_name or base_cmd.startswith(prefix)):
                    return None

                if bot_username and len(command_parts) > 2 and command_parts[1].lower() != bot_username.lower():
                    return None

                if base_cmd.startswith(prefix) and not args:
                    extracted = base_cmd[len(prefix) :]
                    args = [extracted]

                if not self._check_correct_args(args):
                    return None

                filter_result = self.filters.check_update(update) if self.filters else True
                if filter_result:
                    return args, filter_result
                return False
        return None


class ResumeCommandHandler(SuffixCommandHandler):
    """Handles /resume, /resume <arg>, and clickable /resume_<arg> commands (e.g. /resume_1)."""

    def __init__(self, callback):
        super().__init__("resume", callback)


@restricted
@general_topic_redirect
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback handler for commands starting with /."""
    msg = update.effective_message
    if not msg or not msg.text:
        return

    session = get_session_from_update(update)
    if session.terminal_mode:
        from .chat import execute_terminal_command
        # Forward direct binary invocations (e.g. /bin/ls) to terminal execution
        await execute_terminal_command(update, context, msg.text.strip())
    else:
        await safe_reply(msg, "❓ Unknown command. Send /help for available commands.")
