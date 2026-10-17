"""Chat messages, media processing, terminal execution, and LLM interaction handlers for UziClaw."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional
from telegram import Update
from telegram.ext import ContextTypes

from ..config import INJECT_TELEGRAM_CONTEXT, MEDIA_DIR, FORUM_GROUP_ID
from ..auth import restricted
from ..session_manager import session_manager
from ..formatters import (
    split_message,
    safe_reply,
    safe_send,
    safe_edit,
    format_terminal_output,
    RateLimitedMessageUpdater,
    LiveStatusTracker,
)
from ..tray import notify_tray_state_changed
from ..media_manager import download_telegram_file_with_retry
from .commands import get_session_from_update
from .forum_helper import (
    is_forum_general_chat,
    generate_topic_name,
    format_user_prompt_preview,
    get_topic_icon_id,
    detect_topic_icon_from_prompt,
    clean_prompt_for_display,
    FORUM_TOPIC_ICONS,
    create_forum_topic_with_retry,
    edit_forum_topic_with_retry,
    unpin_topic_messages_quietly,
)

logger = logging.getLogger(__name__)


def _is_context_injection_enabled() -> bool:
    import sys
    bot_mod = sys.modules.get("src.bot")
    if bot_mod and hasattr(bot_mod, "INJECT_TELEGRAM_CONTEXT"):
        return getattr(bot_mod, "INJECT_TELEGRAM_CONTEXT")
    return INJECT_TELEGRAM_CONTEXT


def build_prompt_with_context(prompt_text: str, user=None, is_first_turn: bool = True, chat_id: Optional[int] = None) -> str:
    """Optionally inject context so the CLI agent knows the prompt is coming from Telegram (first turn only)."""
    if not _is_context_injection_enabled() or not is_first_turn:
        return prompt_text

    user_identifier = f"@{user.username}" if (user and getattr(user, "username", None)) else (getattr(user, "first_name", None) or "User")
    target_chat = str(chat_id) if chat_id else (str(user.id) if user and getattr(user, "id", None) else "")
    schedule_hint = (
        f" When the user requests a scheduled or recurring task, register it using: "
        f"'python -m src.schedule_helper add --chat-id {target_chat} --type <cron|once> --expr <cron_expression_or_offset> --desc <description> --prompt <task_prompt>'."
        if target_chat else ""
    )
    context_prefix = (
        f"[Context: Prompt sent by {user_identifier} via Telegram bot. "
        f"You are responding through Telegram chat. "
        f"Keep responses clear, concise, and mobile-friendly where appropriate. "
        f"Avoid expecting interactive terminal input.{schedule_hint}]\n\n"
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
        filename = f"voice_{msg.message_id}_{file_id[:8]}.ogg"
        local_path = MEDIA_DIR / filename
        await download_telegram_file_with_retry(context.bot, file_id, local_path)
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
        filename = f"photo_{msg.message_id}_{file_id[:8]}.jpg"
        local_path = MEDIA_DIR / filename
        await download_telegram_file_with_retry(context.bot, file_id, local_path)
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
        orig_ext = Path(msg.audio.file_name).suffix if msg.audio.file_name else ".mp3"
        filename = f"audio_{msg.message_id}_{file_id[:8]}{orig_ext}"
        local_path = MEDIA_DIR / filename
        await download_telegram_file_with_retry(context.bot, file_id, local_path)
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
        orig_name = msg.document.file_name or "file"
        safe_name = "".join(c for c in orig_name if c.isalnum() or c in "._- ")
        filename = f"doc_{msg.message_id}_{safe_name}"
        local_path = MEDIA_DIR / filename
        await download_telegram_file_with_retry(context.bot, file_id, local_path)
        caption = msg.caption.strip() if msg.caption else ""
        user_request = caption if caption else f"Please inspect the attached file ({orig_name})."
        prompt_text = (
            f"[Attached Document: {local_path} (Use view_file to inspect)]\n\n"
            f"{user_request}"
        )
        return prompt_text, f"📄 Processing document ({orig_name})..."

    # 5. Plain text
    if msg.text:
        return msg.text.strip(), "🤖 *UziClaw:* Initializing..."

    return "", ""


@restricted
async def cmd_terminal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle or switch direct terminal mode (no LLM)."""
    msg = update.effective_message
    if not msg:
        return

    session = get_session_from_update(update)

    if session.runner.is_running:
        await safe_reply(msg, "⚠️ An agent turn is currently running. Please wait or send /cancel first.")
        return

    arg = context.args[0].lower() if context.args else None
    if arg in ("off", "exit", "quit", "disable", "stop"):
        session.terminal_mode = False
    elif arg in ("on", "enable", "start"):
        session.terminal_mode = True
    else:
        # Toggle mode
        session.terminal_mode = not session.terminal_mode

    if session.terminal_mode:
        text = (
            "💻 **Terminal Mode Activated**\n\n"
            "Every message you send will be executed directly in the shell (`bash`), with **no LLM involved**.\n\n"
            f"• **Working Directory:** `{session.cwd}`\n"
            "• **Exit:** Send `/terminal` or `exit` to return to LLM mode."
        )
    else:
        text = "🔙 **Exited Terminal Mode.** Returned to LLM mode."

    await safe_reply(msg, text)


async def _handle_general_forum_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg, chat) -> None:
    """
    Handle message sent in the General topic of a forum supergroup.
    Spawns a new dedicated forum topic, runs prompt in a fresh session, and posts ONLY in the new topic.
    Never posts anything in the General topic.
    """
    try:
        prompt_text, init_status = await prepare_prompt_from_message(update, context)
    except Exception as e:
        logger.error("Failed to process media in General forum message: %s", e, exc_info=True)
        return

    if not prompt_text:
        return

    topic_name = generate_topic_name(prompt_text)
    icon_id = get_topic_icon_id(prompt_text)
    topic = await create_forum_topic_with_retry(
        bot=context.bot,
        chat_id=chat.id,
        name=topic_name,
        icon_custom_emoji_id=icon_id,
    )
    target_thread_id = getattr(topic, "message_thread_id", None)
    if target_thread_id is None:
        logger.error("Failed to create forum topic for General message in chat %s after retries", chat.id)
        return

    # Dedicated fresh session for this new topic
    session = session_manager.get_session(chat.id, target_thread_id)
    session.reset_conversation()
    session_manager.save_session(session)

    # Post user prompt preview in the new topic
    try:
        preview = format_user_prompt_preview(msg, prompt_text)
        await safe_send(context.bot, chat.id, preview, thread_id=target_thread_id)
    except Exception as e:
        logger.debug("Failed to post prompt preview to new topic: %s", e)

    # Post initial status message in the new topic
    status_msg = await safe_send(context.bot, chat.id, init_status, thread_id=target_thread_id)

    # Automatically unpin any messages pinned by default in the new topic
    await unpin_topic_messages_quietly(context.bot, chat.id, target_thread_id)

    # Run agent turn inside the topic
    acquired = False
    my_gen = session.queue_generation
    session.queued_count += 1
    try:
        async with session.lock:
            acquired = True
            session.queued_count = max(0, session.queued_count - 1)
            if session.queue_generation != my_gen:
                return

            session.is_busy = True
            session.cancel_requested = False
            current_runner = session.runner
            try:
                updater = RateLimitedMessageUpdater(status_msg, min_interval=1.5) if status_msg else None
                tracker = LiveStatusTracker(updater) if updater else None

                async def on_event(event_type: str, data: Any) -> None:
                    if tracker:
                        await tracker.on_event(event_type, data)

                full_prompt = build_prompt_with_context(
                    prompt_text,
                    user=update.effective_user,
                    is_first_turn=True,
                    chat_id=chat.id,
                )
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

                if result.get("conversation_id"):
                    if session.runner is current_runner:
                        session.conversation_id = result["conversation_id"]
                        session_manager.save_session(session)

                status = result.get("status")
                response_text = result.get("response", "").strip()
                error_msg = result.get("error", "")

                if status == "CANCELLED":
                    cancel_text = response_text or "⏹️ Execução cancelada ou interrompida."
                    if updater:
                        await updater.update(cancel_text, force=True)
                    else:
                        await safe_send(context.bot, chat.id, cancel_text, thread_id=target_thread_id)
                    return

                if status == "ERROR":
                    err_display = f"❌ **Error executing turn:**\n```\n{error_msg}\n```"
                    if updater:
                        await updater.update(err_display, force=True)
                    else:
                        await safe_send(context.bot, chat.id, err_display, thread_id=target_thread_id)
                    return

                if not response_text:
                    if updater:
                        await updater.update("⚠️ Turn completed with empty output.", force=True)
                    else:
                        await safe_send(context.bot, chat.id, "⚠️ Turn completed with empty output.", thread_id=target_thread_id)
                    return

                chunks = split_message(response_text)
                edited = False
                if updater:
                    edited = await updater.update(chunks[0], force=True)
                if not edited:
                    await safe_send(context.bot, chat.id, chunks[0], thread_id=target_thread_id)

                for chunk in chunks[1:]:
                    await safe_send(context.bot, chat.id, chunk, thread_id=target_thread_id)

                # If topic was initially created with generic voice placeholder, refine with detected icon & topic
                if topic_name.startswith("🎙️ Áudio") and response_text:
                    try:
                        em, refined_icon_id = detect_topic_icon_from_prompt(response_text)
                        cand_lines = [l.strip() for l in clean_prompt_for_display(response_text).splitlines() if l.strip() and not l.strip().startswith(("#", "-", "*", ">"))]
                        first_cand = cand_lines[0] if cand_lines else ""
                        first_cand = re.sub(r"[*_`#~\[\]\(\)]", "", first_cand).strip()
                        first_cand = re.sub(r"^([0-9]+\.|\-|\*)\s*", "", first_cand).strip()
                        if first_cand:
                            refined_title = f"{em} {first_cand}"
                            if len(refined_title) > 120:
                                refined_title = refined_title[:117] + "..."
                            await edit_forum_topic_with_retry(
                                bot=context.bot,
                                chat_id=chat.id,
                                message_thread_id=target_thread_id,
                                name=refined_title[:128],
                                icon_custom_emoji_id=refined_icon_id,
                            )
                    except Exception as ex:
                        logger.debug("Failed to auto-refine voice topic: %s", ex)
            finally:
                if session.runner is current_runner:
                    session.is_busy = False
    finally:
        if not acquired:
            session.queued_count = max(0, session.queued_count - 1)


async def execute_terminal_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Execute a shell command directly and reply with formatted output."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    is_general = is_forum_general_chat(chat, msg)
    is_private = (chat.type == "private")

    if is_general:
        # Create topic for terminal command, never post to General
        cmd_preview = command if len(command) <= 50 else command[:47] + "..."
        topic_name = f"💻 $ {cmd_preview}"
        topic = await create_forum_topic_with_retry(
            bot=context.bot,
            chat_id=chat.id,
            name=topic_name,
            icon_custom_emoji_id=FORUM_TOPIC_ICONS["terminal"],
        )
        target_thread_id = getattr(topic, "message_thread_id", None)
        if target_thread_id is None:
            logger.error("Failed to create topic for terminal command in General: %s", chat.id)
            return

        session = session_manager.get_session(chat.id, target_thread_id)
        current_terminal_runner = session.terminal_runner
        session.is_busy = True
        session.cancel_requested = False
        try:
            status_msg = await safe_send(context.bot, chat.id, f"💻 `$ {cmd_preview}`\n⏳ _Running..._", thread_id=target_thread_id)
            await unpin_topic_messages_quietly(context.bot, chat.id, target_thread_id)
            result = await session.terminal_runner.run(command=command, cwd=session.cwd)
            if result.get("new_cwd"):
                session.cwd = result["new_cwd"]
                session_manager.save_session(session)

            chunks = format_terminal_output(command, result) or ["✅ _(No output)_"]
            if status_msg:
                edited = await safe_edit(status_msg, chunks[0])
                if not edited:
                    await safe_send(context.bot, chat.id, chunks[0], thread_id=target_thread_id)
            else:
                await safe_send(context.bot, chat.id, chunks[0], thread_id=target_thread_id)

            for chunk in chunks[1:]:
                await safe_send(context.bot, chat.id, chunk, thread_id=target_thread_id)
        finally:
            if session.terminal_runner is current_terminal_runner:
                session.is_busy = False
        return

    session = get_session_from_update(update)
    current_terminal_runner = session.terminal_runner

    session.is_busy = True
    session.cancel_requested = False
    notify_tray_state_changed()
    try:
        cmd_preview = command if len(command) <= 50 else command[:47] + "..."
        status_msg = await safe_reply(msg, f"💻 `$ {cmd_preview}`\n⏳ _Running..._")

        if session.cancel_requested:
            session.cancel_requested = False
            if status_msg:
                await safe_edit(status_msg, "⏹️ Execution cancelled before starting.")
            else:
                await safe_reply(msg, "⏹️ Execution cancelled before starting.")
            return

        result = await session.terminal_runner.run(command=command, cwd=session.cwd)

        if result.get("new_cwd"):
            session.cwd = result["new_cwd"]

        chunks = format_terminal_output(command, result)
        if not chunks:
            chunks = ["✅ _(No output)_"]

        if status_msg:
            edited = await safe_edit(status_msg, chunks[0])
            if not edited:
                await safe_reply(msg, chunks[0])
        else:
            await safe_reply(msg, chunks[0])

        for chunk in chunks[1:]:
            await safe_reply(msg, chunk)
    finally:
        if session.terminal_runner is current_terminal_runner:
            session.is_busy = False
            notify_tray_state_changed()


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle normal prompt messages and media (photos, voice notes, audio, documents)."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    # Check if sent in the General topic of a forum group
    if is_forum_general_chat(chat, msg):
        await _handle_general_forum_message(update, context, msg, chat)
        return

    is_private = (chat.type == "private")
    session = get_session_from_update(update)

    # 1. Direct terminal mode (no LLM involved)
    if session.terminal_mode:
        # Handle media gracefully
        if msg.photo or msg.voice or msg.audio or msg.document:
            await safe_reply(
                msg,
                "⚠️ Terminal mode is active. Only shell text commands are supported.\n"
                "Send `/terminal` to exit and return to LLM mode.",
            )
            return

        cmd_text = msg.text.strip() if msg.text else ""
        if not cmd_text:
            return

        # Check for explicit exit command
        if cmd_text.lower() in ("exit", "quit", ":q") or cmd_text.lower().startswith("/terminal"):
            session.terminal_mode = False
            await safe_reply(msg, "🔙 **Exited Terminal Mode.** Returned to LLM mode.")
            return

        acquired = False
        my_gen = session.queue_generation
        session.queued_count += 1
        try:
            async with session.lock:
                acquired = True
                session.queued_count = max(0, session.queued_count - 1)
                if session.queue_generation != my_gen:
                    logger.info("Dropping queued terminal command cancelled during wait.")
                    return
                await execute_terminal_command(update, context, cmd_text)
        finally:
            if not acquired:
                session.queued_count = max(0, session.queued_count - 1)
        return

    # Normal LLM mode with silent message queueing
    acquired = False
    my_gen = session.queue_generation
    session.queued_count += 1
    try:
        async with session.lock:
            acquired = True
            session.queued_count = max(0, session.queued_count - 1)
            if session.queue_generation != my_gen:
                logger.info("Dropping queued message cancelled during wait.")
                return

            session.is_busy = True
            session.cancel_requested = False
            current_runner = session.runner
            notify_tray_state_changed()
            try:
                # Extract prompt text and handle media downloads
                try:
                    prompt_text, init_status = await prepare_prompt_from_message(update, context)
                except Exception as e:
                    logger.error("Failed to process message media: %s", e, exc_info=True)
                    await safe_reply(msg, f"❌ Failed to process attached file: {e}")
                    return

                if not prompt_text:
                    return

                if session.cancel_requested:
                    session.cancel_requested = False
                    await safe_reply(msg, "⏹️ Execution cancelled before starting.")
                    return

                # Post initial status message with retry
                status_msg = await safe_reply(msg, init_status)
                if not status_msg:
                    await asyncio.sleep(1)
                    status_msg = await safe_reply(msg, init_status)

                if session.cancel_requested:
                    session.cancel_requested = False
                    if status_msg:
                        await safe_edit(status_msg, "⏹️ Execution cancelled before starting.")
                    else:
                        await safe_reply(msg, "⏹️ Execution cancelled before starting.")
                    return

                updater = RateLimitedMessageUpdater(status_msg, min_interval=1.5) if status_msg else None
                tracker = LiveStatusTracker(updater) if updater else None

                async def on_event(event_type: str, data: Any) -> None:
                    if tracker:
                        await tracker.on_event(event_type, data)

                # Run the prompt with injected Telegram context if enabled (first turn only)
                is_first_turn = session.conversation_id is None
                full_prompt = build_prompt_with_context(
                    prompt_text,
                    user=update.effective_user,
                    is_first_turn=is_first_turn,
                    chat_id=update.effective_chat.id,
                )
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

                # Update session conversation ID and persist (guard against detached runs if /new was called)
                if result.get("conversation_id"):
                    if session.runner is current_runner:
                        session.conversation_id = result["conversation_id"]
                        session_manager.save_session(session)

                status = result.get("status")
                response_text = result.get("response", "").strip()
                error_msg = result.get("error", "")

                if status == "CANCELLED":
                    cancel_text = response_text or "⏹️ Execução cancelada ou interrompida."
                    if updater:
                        await updater.update(cancel_text, force=True)
                    else:
                        await safe_reply(msg, cancel_text)
                    return

                if status == "ERROR":
                    err_display = f"❌ **Error executing turn:**\n```\n{error_msg}\n```"
                    if updater:
                        await updater.update(err_display, force=True)
                    else:
                        await safe_reply(msg, err_display)
                    return

                if not response_text:
                    empty_msg = "⚠️ Turn completed with empty output."
                    if updater:
                        await updater.update(empty_msg, force=True)
                    else:
                        await safe_reply(msg, empty_msg)
                    return

                # Response is available: chunk it and send
                chunks = split_message(response_text)

                # Edit initial message with the first chunk, or send as reply if no status_msg
                edited = False
                if updater:
                    edited = await updater.update(chunks[0], force=True)
                if not edited:
                    await safe_reply(msg, chunks[0])

                # Send any remaining chunks as separate messages
                for chunk in chunks[1:]:
                    await safe_reply(msg, chunk)
            finally:
                if session.runner is current_runner:
                    session.is_busy = False
                    notify_tray_state_changed()
    finally:
        if not acquired:
            session.queued_count = max(0, session.queued_count - 1)
