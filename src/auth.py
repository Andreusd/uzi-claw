"""Authorization and user access validation."""

import functools
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes

from .config import AUTHORIZED_USERS_FILE

logger = logging.getLogger(__name__)

# In-memory authorized users cache
_AUTHORIZED_USERS: set[str] = set()


def load_authorized_users() -> set[str]:
    """Load authorized usernames/ids strictly from authorized_users.txt."""
    users = set()

    if AUTHORIZED_USERS_FILE.exists():
        try:
            with AUTHORIZED_USERS_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    users.add(line.lstrip("@"))
        except Exception as e:
            logger.error("Failed to read %s: %s", AUTHORIZED_USERS_FILE, e)
    else:
        logger.warning("Authorized users file not found: %s", AUTHORIZED_USERS_FILE)

    _AUTHORIZED_USERS.clear()
    _AUTHORIZED_USERS.update(users)
    logger.info("Loaded %d authorized user identifier(s) from %s: %s", len(_AUTHORIZED_USERS), AUTHORIZED_USERS_FILE.name, _AUTHORIZED_USERS)
    return _AUTHORIZED_USERS


def is_user_authorized(user) -> bool:
    """Check if the given Telegram user is authorized."""
    if not user:
        return False

    # Always reload to reflect changes in authorized_users.txt immediately
    load_authorized_users()

    user_id_str = str(user.id)
    username = (user.username or "").lstrip("@")

    logger.info("User auth check: id=%s username=%s", user_id_str, username)

    # Check numeric ID or username
    authorized_lower = {u.lower() for u in _AUTHORIZED_USERS}

    if user_id_str in _AUTHORIZED_USERS:
        return True
    if username and username.lower() in authorized_lower:
        return True

    return False


def restricted(func):
    """Decorator to restrict handler execution to authorized users in private chats only."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # Enforce private DM chat only (ignore groups and channels completely)
        chat = update.effective_chat
        if not chat or chat.type != "private":
            logger.warning("Interaction dropped from non-private chat: type=%s", getattr(chat, 'type', None))
            return

        user = update.effective_user
        if not user or not is_user_authorized(user):
            user_info = f"id={getattr(user, 'id', None)}, username={getattr(user, 'username', None)}"
            logger.warning("Unauthorized access attempt silently dropped (stealth mode): %s", user_info)
            # Do not answer or send any message so unauthorized users think the bot is dead
            return
        return await func(update, context, *args, **kwargs)

    return wrapper
