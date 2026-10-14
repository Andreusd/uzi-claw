"""Authorization and user access validation."""

import functools
import html
import logging
import time
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes

from .config import AUTHORIZED_USERS_FILE

logger = logging.getLogger(__name__)

# In-memory authorized users cache
_AUTHORIZED_USERS: set[str] = set()

# Rate limiting for unauthorized alerts: user_id -> list of timestamp floats
_ALERT_TIMESTAMPS: dict[int, list[float]] = {}
_MAX_ALERTS_PER_WINDOW = 5
_ALERT_WINDOW_SECONDS = 60.0


def _should_throttle_alert(user_id: int) -> bool:
    """Check if alerts for this user should be throttled to prevent spam."""
    now = time.time()
    timestamps = _ALERT_TIMESTAMPS.setdefault(user_id, [])
    # Keep only timestamps within the sliding window
    timestamps = [t for t in timestamps if now - t < _ALERT_WINDOW_SECONDS]
    _ALERT_TIMESTAMPS[user_id] = timestamps

    if len(timestamps) >= _MAX_ALERTS_PER_WINDOW:
        return True

    timestamps.append(now)
    return False


def _extract_message_content(msg) -> str:
    """Extract human-readable content or media description from a message."""
    if not msg:
        return "(No message content)"

    content_parts = []

    if getattr(msg, "text", None):
        content_parts.append(msg.text)
    elif getattr(msg, "caption", None):
        content_parts.append(msg.caption)

    # Detect media types
    media_types = []
    if getattr(msg, "photo", None):
        media_types.append("Photo")
    if getattr(msg, "voice", None):
        media_types.append("Voice Note")
    if getattr(msg, "audio", None):
        orig = f" ({msg.audio.file_name})" if getattr(msg.audio, "file_name", None) else ""
        media_types.append(f"Audio{orig}")
    if getattr(msg, "document", None):
        orig = f" ({msg.document.file_name})" if getattr(msg.document, "file_name", None) else ""
        media_types.append(f"Document{orig}")
    if getattr(msg, "video", None):
        media_types.append("Video")
    if getattr(msg, "sticker", None):
        emoji = f" {msg.sticker.emoji}" if getattr(msg.sticker, "emoji", None) else ""
        media_types.append(f"Sticker{emoji}")
    if getattr(msg, "contact", None):
        media_types.append("Contact")
    if getattr(msg, "location", None):
        media_types.append("Location")

    if media_types:
        media_desc = f"[{', '.join(media_types)}]"
        if content_parts:
            content_parts.insert(0, media_desc)
        else:
            content_parts.append(media_desc)

    text = "\n".join(content_parts).strip()
    return text or "(Non-text content)"


async def notify_unauthorized_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an alert to all authorized users when an unauthorized user messages the bot."""
    if not context or not getattr(context, "bot", None):
        return

    user = update.effective_user
    user_id = getattr(user, "id", None) or 0

    if user_id and _should_throttle_alert(user_id):
        logger.warning("Throttled unauthorized access notification for user id=%s", user_id)
        return

    # User representation
    full_name = getattr(user, "full_name", None) or (
        f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() or "Unknown"
    )
    username = f"@{user.username}" if getattr(user, "username", None) else "None"

    # Message representation
    msg = update.effective_message
    content = _extract_message_content(msg)

    # Truncate content if excessively long (Telegram message limit is 4096)
    if len(content) > 3000:
        content = content[:3000] + "\n...[truncated]"

    alert_text = (
        "🚨 <b>Unauthorized Access Attempt</b>\n\n"
        f"👤 <b>Sender:</b> {html.escape(full_name)} ({html.escape(username)})\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        "💬 <b>Content:</b>\n"
        f"<pre>{html.escape(content)}</pre>"
    )

    # Broadcast to all authorized numeric chat IDs
    authorized_list = load_authorized_users()
    for uid_str in authorized_list:
        try:
            admin_chat_id = int(uid_str)
        except ValueError:
            continue

        try:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=alert_text,
                parse_mode="HTML",
            )
            logger.info("Sent unauthorized access alert to authorized user %s", admin_chat_id)
        except Exception as e:
            logger.error("Failed to send unauthorized alert to admin %s: %s", admin_chat_id, e)


_LAST_LOADED_MTIME: float = 0.0


def load_authorized_users(force: bool = False) -> set[str]:
    """Load authorized usernames/ids strictly from authorized_users.txt with mtime caching."""
    global _AUTHORIZED_USERS, _LAST_LOADED_MTIME

    if not AUTHORIZED_USERS_FILE.exists():
        logger.warning("Authorized users file not found: %s", AUTHORIZED_USERS_FILE)
        return _AUTHORIZED_USERS

    try:
        current_mtime = AUTHORIZED_USERS_FILE.stat().st_mtime
    except Exception:
        current_mtime = 0.0

    if not force and current_mtime == _LAST_LOADED_MTIME and _AUTHORIZED_USERS:
        return _AUTHORIZED_USERS

    users = set()
    try:
        with AUTHORIZED_USERS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                users.add(line.lstrip("@"))
    except Exception as e:
        logger.error("Failed to read %s: %s", AUTHORIZED_USERS_FILE, e)

    _AUTHORIZED_USERS.clear()
    _AUTHORIZED_USERS.update(users)
    _LAST_LOADED_MTIME = current_mtime
    logger.debug(
        "Loaded %d authorized user identifier(s) from %s: %s",
        len(_AUTHORIZED_USERS),
        AUTHORIZED_USERS_FILE.name,
        _AUTHORIZED_USERS,
    )
    return _AUTHORIZED_USERS


def is_user_authorized(user) -> bool:
    """Check if the given Telegram user is authorized."""
    if not user:
        return False

    load_authorized_users()

    user_id_str = str(user.id)
    username = (user.username or "").lstrip("@")

    authorized_list = _AUTHORIZED_USERS
    authorized_lower = {u.lower() for u in authorized_list}

    is_auth = user_id_str in authorized_list or (username and username.lower() in authorized_lower)
    logger.debug("User auth check: id=%s username=%s authorized=%s", user_id_str, username, is_auth)
    return is_auth


def restricted(func):
    """Decorator to restrict handler execution to authorized users (supports private DMs and forum topics)."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        if not chat or chat.type not in ("private", "group", "supergroup"):
            logger.warning("Interaction dropped from unsupported chat type: %s", getattr(chat, 'type', None))
            return

        user = update.effective_user
        if not user or not is_user_authorized(user):
            user_info = f"id={getattr(user, 'id', None)}, username={getattr(user, 'username', None)}"
            logger.warning("Unauthorized access attempt silently dropped (stealth mode): %s", user_info)
            try:
                await notify_unauthorized_access(update, context)
            except Exception as e:
                logger.error("Failed to notify authorized users of unauthorized access: %s", e, exc_info=True)
            # Do not answer or send any message so unauthorized users think the bot is dead
            return
        return await func(update, context, *args, **kwargs)

    return wrapper
