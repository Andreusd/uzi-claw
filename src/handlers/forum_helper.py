"""Forum topics helpers, isolation logic, and title generation for UziClaw."""

import logging
import re
from datetime import datetime
from typing import Optional
from telegram import Message, Chat

from ..config import FORUM_GROUP_ID

logger = logging.getLogger(__name__)


def is_forum_general_chat(chat: Optional[Chat], msg: Optional[Message]) -> bool:
    """
    Check if a message was sent to the General topic of a Telegram forum supergroup.
    In Telegram Bot API, the General topic in a forum supergroup is identified when
    chat.is_forum is True, chat.type is 'supergroup', and message_thread_id is None or 1.
    """
    if not chat:
        return False

    chat_type = getattr(chat, "type", None)
    if chat_type and str(chat_type).lower() != "supergroup":
        return False

    is_forum = getattr(chat, "is_forum", False)
    if is_forum is not True:
        return False

    thread_id = getattr(msg, "message_thread_id", None) if msg else None
    is_topic = getattr(msg, "is_topic_message", False) if msg else False

    # A custom forum topic has is_topic=True and thread_id > 1
    if is_topic is True and thread_id is not None and thread_id > 1:
        return False

    return True


def clean_prompt_for_display(prompt_text: str) -> str:
    """Clean internal prompt prefixes, context injection tags, and metadata."""
    if not prompt_text:
        return ""
    text = prompt_text
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    text = re.sub(r"^\[Context:.*?\]\s*", "", text, flags=re.DOTALL).strip()
    return text


def generate_topic_name(prompt_text: str, default_prefix: str = "Conversa") -> str:
    """
    Generate a clean, readable topic name for Telegram forum topic (max 128 UTF-8 characters).
    """
    cleaned = clean_prompt_for_display(prompt_text)
    now_str = datetime.now().strftime("%d/%m %H:%M")

    if not cleaned:
        return f"💬 {default_prefix} {now_str}"

    if cleaned.startswith("[Attached Voice Note:"):
        if "User's note:" in cleaned:
            note = cleaned.split("User's note:")[-1].strip()
            title = f"🎙️ {note}"
        else:
            title = f"🎙️ Áudio {now_str}"
        return title[:128]

    if cleaned.startswith("[Attached Image:"):
        # Check if there is user text
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("[Attached Image:")]
        if lines:
            title = f"🖼️ {lines[0]}"
        else:
            title = f"🖼️ Imagem {now_str}"
        return title[:128]

    if cleaned.startswith("[Attached Audio File:"):
        return f"🎵 Áudio {now_str}"[:128]

    if cleaned.startswith("[Attached Document:"):
        doc_m = re.search(r"doc_\d+_(.+?)(?:\s+\(Use view_file|\n)", cleaned)
        if doc_m:
            title = f"📄 {doc_m.group(1)}"
        else:
            title = f"📄 Documento {now_str}"
        return title[:128]

    # Plain text: pick first non-empty line
    first_line = ""
    for line in cleaned.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break

    # Strip markdown symbols
    first_line = re.sub(r"[*_`#~\[\]\(\)]", "", first_line).strip()
    if not first_line:
        first_line = f"{default_prefix} {now_str}"

    title = f"💬 {first_line}"
    if len(title) > 120:
        title = title[:117] + "..."
    return title[:128]


def format_user_prompt_preview(msg: Optional[Message], prompt_text: str, origin: Optional[str] = None) -> str:
    """Format a clean user prompt preview to be displayed inside a forum topic."""
    cleaned = clean_prompt_for_display(prompt_text)
    user_label = f"👤 **User{f' ({origin})' if origin else ''}:**"

    if not cleaned:
        return f"{user_label} _(Sem texto)_"

    if cleaned.startswith("[Attached Voice Note:"):
        note = ""
        if "User's note:" in cleaned:
            note = f"\n_Nota: {cleaned.split('User\'s note:')[-1].strip()}_"
        return f"{user_label} 🎙️ _(Mensagem de voz){note}_"

    if cleaned.startswith("[Attached Image:"):
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("[Attached Image:")]
        extra = f"\n_{lines[0]}_" if lines else ""
        return f"{user_label} 🖼️ _(Imagem){extra}_"

    if cleaned.startswith("[Attached Document:"):
        return f"{user_label} 📄 _(Documento anexado)_"

    if cleaned.startswith("[Attached Audio File:"):
        return f"{user_label} 🎵 _(Arquivo de áudio)_"

    # Truncate text if very long for initial header
    preview = cleaned if len(cleaned) <= 1000 else cleaned[:997] + "..."
    return f"{user_label}\n{preview}"
