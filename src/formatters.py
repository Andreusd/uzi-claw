"""Formatting, chunking, and rate-limited message editing for Telegram."""

import asyncio
import html
import logging
import re
import time
from datetime import datetime
from typing import Optional, List
from telegram import Message
from telegram.error import BadRequest, RetryAfter

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 3500

VALID_TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "tg-spoiler", "a", "tg-emoji", "span"
}


def sanitize_and_balance_html(html_text: str) -> str:
    """
    Ensure all Telegram HTML tags are properly balanced and unclosed tags are closed.
    Discards orphaned closing tags to prevent entity parsing errors in Telegram.
    """
    tag_pattern = re.compile(r"</?([a-zA-Z0-9_\-]+)(?:\s+[^>]*)?>")
    stack = []
    cleaned_parts = []
    last_idx = 0

    for match in tag_pattern.finditer(html_text):
        tag_str = match.group(0)
        tag_name = match.group(1).lower()
        start, end = match.span()
        cleaned_parts.append(html_text[last_idx:start])
        last_idx = end

        if tag_str.startswith("</"):
            if tag_name in stack:
                while stack and stack[-1] != tag_name:
                    unclosed = stack.pop()
                    cleaned_parts.append(f"</{unclosed}>")
                if stack and stack[-1] == tag_name:
                    stack.pop()
                    cleaned_parts.append(f"</{tag_name}>")
            else:
                # Discard orphaned closing tag
                pass
        else:
            stack.append(tag_name)
            cleaned_parts.append(tag_str)

    cleaned_parts.append(html_text[last_idx:])
    while stack:
        unclosed = stack.pop()
        cleaned_parts.append(f"</{unclosed}>")

    return "".join(cleaned_parts)


def markdown_to_telegram_html(text: str) -> str:
    """
    Convert standard Markdown (LLM-style headers, bold, bullets, code blocks, links)
    into Telegram-compatible HTML formatting.
    """
    if not text:
        return ""

    # 1. Extract and protect fenced code blocks (```lang ... ```)
    code_blocks = []
    def save_code_block(m):
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        escaped_code = html.escape(code)
        if lang:
            replacement = f'<pre><code class="language-{lang}">{escaped_code}</code></pre>'
        else:
            replacement = f'<pre>{escaped_code}</pre>'
        code_blocks.append(replacement)
        return f"\x00CB{len(code_blocks)-1}\x00"

    text = re.sub(r"```([a-zA-Z0-9_\-+]*)\n?(.*?)\n?```", save_code_block, text, flags=re.DOTALL)

    # 2. Extract and protect inline code (`...`)
    inline_codes = []
    def save_inline_code(m):
        code = m.group(1)
        escaped_code = html.escape(code)
        inline_codes.append(f"<code>{escaped_code}</code>")
        return f"\x00IC{len(inline_codes)-1}\x00"

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 3. Protect any existing valid Telegram HTML tags
    valid_tag_pattern = re.compile(
        r"</?(?:" + "|".join(VALID_TELEGRAM_TAGS) + r")(?:\s+[a-zA-Z\-]+=\"[^\"]*\")*\s*/?>",
        re.IGNORECASE
    )
    valid_tags = []
    def save_valid_tag(m):
        valid_tags.append(m.group(0))
        return f"\x00TAG{len(valid_tags)-1}\x00"

    text = valid_tag_pattern.sub(save_valid_tag, text)

    # Clean legacy backslash escapes from strings like \* or \_
    text = text.replace(r"\*", "*").replace(r"\_", "_").replace(r"\[", "[").replace(r"\]", "]").replace(r"\`", "`")

    # 4. Escape remaining raw characters (&, <, >)
    text = html.escape(text)

    # 5. Restore valid tags
    for i, tag in enumerate(valid_tags):
        text = text.replace(f"\x00TAG{i}\x00", tag)

    # 6. Headers: # Heading -> <b>Heading</b>
    text = re.sub(r"^(#{1,6})\s+(.+)$", r"<b>\2</b>", text, flags=re.MULTILINE)

    # 7. Horizontal rules: ---, ***, ___
    text = re.sub(r"^([-*_]){3,}\s*$", r"───────────────", text, flags=re.MULTILINE)

    # 8. Bullet list items: * item or - item -> • item
    text = re.sub(r"^(\s*)[*+-]\s+", r"\1• ", text, flags=re.MULTILINE)

    # 9. Blockquotes: merge consecutive lines starting with &gt; into <blockquote>...</blockquote>
    lines = text.split("\n")
    out_lines = []
    in_quote = False
    quote_buf = []
    for line in lines:
        if line.startswith("&gt;"):
            in_quote = True
            content = line[4:].lstrip() if line.startswith("&gt; ") else line[4:]
            quote_buf.append(content)
        else:
            if in_quote:
                out_lines.append(f"<blockquote>{chr(10).join(quote_buf)}</blockquote>")
                quote_buf = []
                in_quote = False
            out_lines.append(line)
    if in_quote:
        out_lines.append(f"<blockquote>{chr(10).join(quote_buf)}</blockquote>")
    text = "\n".join(out_lines)

    # 10. Bold & Italic: ***text***
    text = re.sub(r"\*\*\*([^\*\n]+?)\*\*\*", r"<b><i>\1</i></b>", text)

    # 11. Bold: **text** and __text__
    text = re.sub(r"\*\*([^\*\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__([^\n]+?)__(?!\w)", r"<b>\1</b>", text)

    # 12. Strikethrough: ~~text~~
    text = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", text)

    # 13. Links: [text](url)
    def replace_link(m):
        link_text = m.group(1)
        url = m.group(2)
        clean_url = html.escape(html.unescape(url), quote=True)
        return f'<a href="{clean_url}">{link_text}</a>'
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", replace_link, text)

    # 14. Italic: *text* and _text_
    text = re.sub(r"(?<!\*)\*([^\*\s\n](?:[^\*\n]*?[^\*\s\n])?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![a-zA-Z0-9_])_([^_\s\n](?:[^_\n]*?[^_\s\n])?)_(?![a-zA-Z0-9_])", r"<i>\1</i>", text)

    # 15. Restore inline code and code blocks
    for i, ic in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", ic)
    for i, cb in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", cb)

    # 16. Sanitize and balance HTML tags
    return sanitize_and_balance_html(text)


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


async def safe_reply(msg: Message, text: str, parse_mode: Optional[str] = "HTML") -> Optional[Message]:
    """Send reply text, automatically converting Markdown to Telegram HTML and falling back to plain text."""
    if not text:
        return None
    try:
        if parse_mode in ("HTML", "Markdown"):
            formatted_text = markdown_to_telegram_html(text)
            if len(formatted_text) > 4000:
                formatted_text = sanitize_and_balance_html(formatted_text[:3980] + "...")
            return await msg.reply_text(formatted_text, parse_mode="HTML")
        else:
            return await msg.reply_text(text, parse_mode=parse_mode)
    except BadRequest as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "tag" in err_str or "formatting" in err_str or "entity" in err_str:
            logger.debug("Formatting parse failed (%s), falling back to plain text", e)
            plain_text = text if len(text) <= 4000 else text[:3980] + "..."
            return await msg.reply_text(plain_text, parse_mode=None)
        logger.warning("BadRequest replying to message: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to reply: %s", e)
        return None


async def safe_edit(msg: Message, text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """Edit message text, automatically converting Markdown to Telegram HTML and falling back to plain text."""
    try:
        if parse_mode in ("HTML", "Markdown"):
            formatted_text = markdown_to_telegram_html(text)
            edit_text = (
                formatted_text
                if len(formatted_text) <= TELEGRAM_MAX_LENGTH
                else formatted_text[: TELEGRAM_MAX_LENGTH - 10] + "..."
            )
            edit_text = sanitize_and_balance_html(edit_text)
            await msg.edit_text(edit_text, parse_mode="HTML")
            return True
        else:
            edit_text = text if len(text) <= TELEGRAM_MAX_LENGTH else text[:TELEGRAM_MAX_LENGTH - 10] + "..."
            await msg.edit_text(edit_text, parse_mode=parse_mode)
            return True
    except BadRequest as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return True
        if "can't parse entities" in err_str or "tag" in err_str or "formatting" in err_str or "entity" in err_str:
            logger.debug("Formatting parse failed on edit (%s), falling back to plain text", e)
            try:
                plain_text = text if len(text) <= TELEGRAM_MAX_LENGTH else text[:TELEGRAM_MAX_LENGTH - 10] + "..."
                await msg.edit_text(plain_text, parse_mode=None)
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

    async def update(self, text: str, force: bool = False, parse_mode: Optional[str] = "HTML") -> bool:
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
                if parse_mode in ("HTML", "Markdown"):
                    formatted_text = markdown_to_telegram_html(text)
                    edit_text = (
                        formatted_text
                        if len(formatted_text) <= TELEGRAM_MAX_LENGTH
                        else formatted_text[: TELEGRAM_MAX_LENGTH - 10] + "..."
                    )
                    edit_text = sanitize_and_balance_html(edit_text)
                    await self.message.edit_text(edit_text, parse_mode="HTML")
                else:
                    edit_text = (
                        text
                        if len(text) <= TELEGRAM_MAX_LENGTH
                        else text[: TELEGRAM_MAX_LENGTH - 10] + "..."
                    )
                    await self.message.edit_text(edit_text, parse_mode=parse_mode)

                self.last_text = text
                self.last_update_time = time.time()
                return True
            except BadRequest as e:
                err_str = str(e).lower()
                if "message is not modified" in err_str:
                    return False
                if "can't parse entities" in err_str or "formatting" in err_str or "tag" in err_str or "entity" in err_str:
                    try:
                        plain_text = (
                            text
                            if len(text) <= TELEGRAM_MAX_LENGTH
                            else text[: TELEGRAM_MAX_LENGTH - 10] + "..."
                        )
                        await self.message.edit_text(plain_text, parse_mode=None)
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
