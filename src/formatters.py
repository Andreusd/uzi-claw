"""Formatting, chunking, and rate-limited message editing for Telegram."""

import asyncio
import logging
import time
from datetime import datetime
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


async def safe_edit(msg: Message, text: str, parse_mode: Optional[str] = "Markdown") -> bool:
    """Edit message text, automatically falling back to plain text if Markdown parsing fails."""
    try:
        edit_text = text if len(text) <= TELEGRAM_MAX_LENGTH else text[:TELEGRAM_MAX_LENGTH - 10] + "..."
        await msg.edit_text(edit_text, parse_mode=parse_mode)
        return True
    except BadRequest as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return True
        if "can't parse entities" in err_str or "tag" in err_str or "formatting" in err_str:
            logger.debug("Markdown parse failed on edit (%s), falling back to plain text", e)
            try:
                edit_text = text if len(text) <= TELEGRAM_MAX_LENGTH else text[:TELEGRAM_MAX_LENGTH - 10] + "..."
                await msg.edit_text(edit_text, parse_mode=None)
                return True
            except Exception:
                return False
        logger.warning("BadRequest editing message: %s", e)
        return False
    except Exception as e:
        logger.error("Failed to edit message: %s", e)
        return False


def format_usage_data(usage_data: dict) -> str:
    """Format structured usage/quota data into a clean, mobile-friendly Telegram Markdown message."""
    lines = ["📊 *Antigravity Quota Usage*\n"]
    groups = usage_data.get("groups", [])
    if not groups:
        return "📊 *Antigravity Quota Usage*\n\n_No quota groups found._"

    for group in groups:
        gname = group.get("name", "Model Group")
        gdesc = group.get("description", "")
        # Sanitize markdown characters in dynamic names
        clean_gname = gname.replace("_", "\\_").replace("*", "\\*")
        lines.append(f"🔹 *{clean_gname}*")

        if gdesc:
            models_info = gdesc.replace("Models within this group: ", "")
            clean_models_info = models_info.replace("_", "\\_").replace("*", "\\*")
            lines.append(f"_{clean_models_info}_")

        for bucket in group.get("buckets", []):
            bname = bucket.get("name", "Limit")
            bname = bname.replace(" Remaining", "")
            clean_bname = bname.replace("_", "\\_").replace("*", "\\*")

            fraction = bucket.get("remaining_fraction", 1.0)
            try:
                fraction = max(0.0, min(1.0, float(fraction)))
            except (ValueError, TypeError):
                fraction = 1.0
            pct = round(fraction * 100)

            filled = round(fraction * 10)
            bar = "█" * filled + "░" * (10 - filled)

            status_icon = "🟢" if pct > 50 else ("🟡" if pct > 20 else "🔴")
            lines.append(f"• {status_icon} *{clean_bname}:* `{pct}%` `[{bar}]`")

            # Reset and refresh time
            reset_time = bucket.get("reset_time", "")
            time_str = ""
            if reset_time:
                try:
                    dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00")).astimezone()
                    time_str = dt.strftime("%Y-%m-%d %H:%M %Z")
                except Exception:
                    time_str = reset_time

            desc = bucket.get("description", "")
            refresh_note = ""
            if "fully refresh in " in desc:
                refresh_note = desc.split("fully refresh in ")[-1].rstrip(".")

            if refresh_note:
                time_part = f" (`{time_str}`)" if time_str else ""
                lines.append(f"  ⏳ Refreshes in {refresh_note}{time_part}")
            elif time_str and pct < 100:
                lines.append(f"  ⏳ Resets at `{time_str}`")

        lines.append("")

    return "\n".join(lines).strip()



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


def format_terminal_output(command: str, result: dict) -> List[str]:
    """Format shell command execution results into Telegram message chunks."""
    status = result.get("status", "SUCCESS")
    cmd_header = f"`$ {command}`"

    if status == "CANCELLED":
        return [f"{cmd_header}\n\n⏹️ *Command cancelled by user.*"]

    if status == "TIMEOUT":
        timeout_val = int(result.get("timeout", 120))
        return [f"{cmd_header}\n\n⏱️ *Command timed out after {timeout_val}s.*"]

    raw_output = (result.get("output") or "").strip()
    exit_code = result.get("exit_code", 0)
    new_cwd = result.get("new_cwd")
    cwd_note = f"\n📁 `{new_cwd}`" if new_cwd else ""

    # Truncate if excessively long (e.g. over 8000 characters) to avoid flooding chat
    if len(raw_output) > 8000:
        raw_output = f"[... output truncated, showing last 4000 characters ...]\n" + raw_output[-4000:]

    clean_output = raw_output.replace("```", "'''")

    if exit_code == 0:
        if not clean_output:
            return [f"{cmd_header}\n\n✅ *Done (exit code 0)*{cwd_note}"]
        footer = cwd_note
    else:
        status_line = f"❌ *Exit code {exit_code}*"
        footer = f"\n{status_line}{cwd_note}" if cwd_note else f"\n{status_line}"
        if not clean_output:
            return [f"{cmd_header}\n\n{status_line}{cwd_note}"]

    max_chunk_len = 3400
    if len(clean_output) <= max_chunk_len:
        msg_text = f"{cmd_header}\n```\n{clean_output}\n```{footer}"
        return [msg_text]

    # Split into chunks, wrapping each chunk in its own code block
    chunks = []
    raw_pieces = split_message(clean_output, max_length=max_chunk_len)
    total_pieces = len(raw_pieces)

    for i, piece in enumerate(raw_pieces):
        header_part = f"{cmd_header}\n" if i == 0 else ""
        footer_part = footer if i == total_pieces - 1 else ""
        chunk_text = f"{header_part}```\n{piece}\n```{footer_part}"
        chunks.append(chunk_text)

    return chunks
