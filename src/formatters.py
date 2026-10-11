"""Formatting, chunking, and rate-limited message editing for Telegram."""

import asyncio
import logging
import time
from typing import Optional, List
from telegram import Message
from telegram.error import BadRequest, RetryAfter

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4000


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> List[str]:
    """
    Split text into chunks smaller than max_length, preserving paragraph and code boundaries where possible.
    """
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Find best split point
        candidate = remaining[:max_length]

        # Try splitting on markdown code block fence first
        fence_idx = candidate.rfind("\n```")
        if fence_idx > max_length // 2:
            split_at = fence_idx + 1
        else:
            # Try splitting on double newline
            dnl_idx = candidate.rfind("\n\n")
            if dnl_idx > max_length // 2:
                split_at = dnl_idx + 2
            else:
                # Try single newline
                nl_idx = candidate.rfind("\n")
                if nl_idx > max_length // 3:
                    split_at = nl_idx + 1
                else:
                    # Try space
                    space_idx = candidate.rfind(" ")
                    if space_idx > max_length // 3:
                        split_at = space_idx + 1
                    else:
                        split_at = max_length

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


async def safe_reply(msg: Message, text: str, parse_mode: Optional[str] = "Markdown") -> Optional[Message]:
    """Send reply text, automatically falling back to plain text if Markdown parsing fails."""
    try:
        return await msg.reply_text(text, parse_mode=parse_mode)
    except BadRequest as e:
        if "can't parse entities" in str(e).lower() or "tag" in str(e).lower():
            logger.debug("Markdown parse failed (%s), falling back to plain text", e)
            return await msg.reply_text(text, parse_mode=None)
        logger.warning("BadRequest replying to message: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to reply: %s", e)
        return None


class RateLimitedMessageUpdater:
    """Helper to update a Telegram status message without exceeding rate limits."""

    def __init__(self, message: Message, min_interval: float = 1.5):
        self.message = message
        self.min_interval = min_interval
        self.last_update_time = time.time()
        self.last_text = message.text or ""
        self._lock = asyncio.Lock()

    async def update(self, text: str, force: bool = False, parse_mode: Optional[str] = "Markdown") -> bool:
        """Update the message text if interval passed or force is True."""
        if not text or text == self.last_text:
            return False

        now = time.time()
        if not force and (now - self.last_update_time < self.min_interval):
            return False

        async with self._lock:
            # Recheck inside lock
            if not force and (time.time() - self.last_update_time < self.min_interval):
                return False

            try:
                # Truncate preview if too long for a single editing bubble
                edit_text = text if len(text) <= TELEGRAM_MAX_LENGTH else text[:TELEGRAM_MAX_LENGTH - 10] + "..."
                await self.message.edit_text(edit_text, parse_mode=parse_mode)
                self.last_text = text
                self.last_update_time = time.time()
                return True
            except BadRequest as e:
                err_str = str(e).lower()
                if "message is not modified" in err_str:
                    return False
                if "can't parse entities" in err_str or "formatting" in err_str:
                    try:
                        await self.message.edit_text(edit_text, parse_mode=None)
                        self.last_text = text
                        self.last_update_time = time.time()
                        return True
                    except Exception:
                        pass
                logger.debug("BadRequest on message edit: %s", e)
                return False
            except RetryAfter as e:
                logger.warning("Telegram RetryAfter hit: sleep %s seconds", e.retry_after)
                await asyncio.sleep(e.retry_after)
                return False
            except Exception as e:
                logger.warning("Error editing status message: %s", e)
                return False
