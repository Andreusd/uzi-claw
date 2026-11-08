"""CLI and library helper tool to send files and media to Telegram chats/topics."""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional, Any, Union

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from telegram import Bot
from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError
from telegram.request import HTTPXRequest

from src.config import TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

# Extensions recognized for automatic media categorization
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}

# Tag pattern to auto-detect [send_file: ...] or [attach: ...] in LLM responses
TAG_REGEX = re.compile(r"\[(send_file|attach):\s*([^\]]+)\]", re.IGNORECASE)


def parse_chat_id(val: Union[str, int]) -> Union[int, str]:
    """Parse chat ID string into int if possible, or retain string identifier."""
    if isinstance(val, int):
        return val
    s = str(val).strip()
    try:
        return int(s)
    except ValueError:
        return s


def parse_media_tags(text: str) -> list[dict[str, Any]]:
    """
    Parse inline media tags from response text.
    Supports formats like:
      [send_file: /path/to/file.ext]
      [send_file: /path/to/file.ext (caption: optional description)]
      [send_file: /path/to/file.ext caption: optional description]
      [attach: /path/to/file.ext]
      [attach: /path/to/file.ext (caption: optional description)]
    """
    results = []
    for match in TAG_REGEX.finditer(text):
        raw_tag = match.group(0)
        content = match.group(2).strip()

        cap_paren = re.search(r"\(caption:\s*(.*?)\)\s*$", content, re.IGNORECASE)
        if cap_paren:
            caption = cap_paren.group(1).strip()
            path_part = content[:cap_paren.start()].strip()
        else:
            cap_plain = re.search(r"(?:,\s*)?caption:\s*(.+)$", content, re.IGNORECASE)
            if cap_plain:
                caption = cap_plain.group(1).strip()
                path_part = content[:cap_plain.start()].strip()
            else:
                caption = None
                path_part = content.strip()

        clean_path = path_part.strip("\"'")
        clean_caption = caption.strip("\"'") if caption else None

        results.append({
            "raw_tag": raw_tag,
            "path": clean_path,
            "caption": clean_caption,
        })
    return results


async def send_media(
    chat_id: Union[int, str],
    file_path: Union[str, Path],
    caption: Optional[str] = None,
    thread_id: Optional[int] = None,
    media_type: str = "auto",
    bot: Optional[Any] = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """
    Send a file or media to a Telegram chat/topic with proper timeouts, retries,
    and automatic fallback to document if specific media upload fails.
    """
    target_chat = parse_chat_id(chat_id)
    resolved_path = Path(file_path).expanduser().resolve()

    if not resolved_path.exists():
        return {
            "status": "error",
            "message": f"Arquivo não encontrado: {resolved_path}",
            "file": str(resolved_path),
            "chat_id": target_chat,
        }
    if not resolved_path.is_file():
        return {
            "status": "error",
            "message": f"O caminho não é um arquivo válido: {resolved_path}",
            "file": str(resolved_path),
            "chat_id": target_chat,
        }
    if resolved_path.stat().st_size == 0:
        return {
            "status": "error",
            "message": f"O arquivo está vazio (0 bytes): {resolved_path}",
            "file": str(resolved_path),
            "chat_id": target_chat,
        }

    # Determine effective media type
    m_type = (media_type or "auto").lower().strip()
    if m_type == "auto":
        ext = resolved_path.suffix.lower()
        if ext in IMAGE_EXTS:
            effective_type = "photo"
        elif ext in AUDIO_EXTS:
            effective_type = "audio"
        elif ext in VIDEO_EXTS:
            effective_type = "video"
        else:
            effective_type = "document"
    elif m_type in ("photo", "image"):
        effective_type = "photo"
    elif m_type in ("audio", "voice"):
        effective_type = "audio"
    elif m_type == "video":
        effective_type = "video"
    elif m_type in ("document", "file", "doc"):
        effective_type = "document"
    else:
        effective_type = "document"

    safe_caption = caption.strip() if caption else None
    if safe_caption and len(safe_caption) > 1024:
        safe_caption = safe_caption[:1020] + "..."

    should_shutdown = False
    active_bot = bot
    if active_bot is None:
        token = TELEGRAM_TOKEN
        if not token:
            return {
                "status": "error",
                "message": "TELEGRAM_TOKEN is not configured in environment or .env",
                "file": str(resolved_path),
                "chat_id": target_chat,
            }
        req = HTTPXRequest(read_timeout=120.0, write_timeout=120.0, connect_timeout=30.0)
        active_bot = Bot(token=token, request=req)
        should_shutdown = True

    base_kwargs: dict[str, Any] = {
        "chat_id": target_chat,
        "read_timeout": 120.0,
        "write_timeout": 120.0,
        "connect_timeout": 30.0,
    }
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id
    if safe_caption:
        base_kwargs["caption"] = safe_caption

    current_type = effective_type
    sent_msg = None
    last_err: Optional[Exception] = None

    try:
        for attempt in range(1, max_retries + 1):
            try:
                with open(resolved_path, "rb") as f:
                    if current_type == "photo":
                        sent_msg = await active_bot.send_photo(photo=f, **base_kwargs)
                    elif current_type == "audio":
                        sent_msg = await active_bot.send_audio(audio=f, **base_kwargs)
                    elif current_type == "video":
                        sent_msg = await active_bot.send_video(video=f, **base_kwargs)
                    else:
                        sent_msg = await active_bot.send_document(document=f, **base_kwargs)
                break
            except BadRequest as e:
                # Fallback to document on BadRequest when sending as photo, audio, or video
                if current_type in ("photo", "audio", "video"):
                    logger.warning(
                        "Sending %s as %s failed (%s). Falling back to document.",
                        resolved_path.name,
                        current_type,
                        e,
                    )
                    current_type = "document"
                    try:
                        with open(resolved_path, "rb") as f:
                            sent_msg = await active_bot.send_document(document=f, **base_kwargs)
                        break
                    except Exception as fallback_err:
                        last_err = fallback_err
                        break
                else:
                    last_err = e
                    break
            except RetryAfter as e:
                last_err = e
                logger.warning(
                    "Telegram RetryAfter %ss on file upload attempt %d/%d",
                    e.retry_after,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(e.retry_after)
            except (TimedOut, NetworkError, httpx.HTTPError, ConnectionError, asyncio.TimeoutError) as e:
                last_err = e
                logger.warning("File upload attempt %d/%d failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    await asyncio.sleep(attempt * 1.5)
            except Exception as e:
                last_err = e
                break

        if sent_msg:
            return {
                "status": "success",
                "chat_id": target_chat,
                "thread_id": thread_id,
                "file": str(resolved_path),
                "filename": resolved_path.name,
                "type": current_type,
                "message_id": getattr(sent_msg, "message_id", None),
                "caption": safe_caption,
            }
        else:
            return {
                "status": "error",
                "message": str(last_err or "Falha ao enviar arquivo após tentativas"),
                "file": str(resolved_path),
                "chat_id": target_chat,
            }
    finally:
        if should_shutdown and active_bot:
            try:
                await active_bot.shutdown()
            except Exception:
                pass


async def dispatch_media_tags(
    response_text: str,
    bot: Any,
    chat_id: Union[int, str],
    thread_id: Optional[int] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Search response_text for [send_file: ...] or [attach: ...] tags, dispatch them
    via send_media, and return (cleaned_response_text, dispatched_results).
    """
    tags = parse_media_tags(response_text)
    if not tags:
        return response_text, []

    sent_results = []
    cleaned_text = response_text
    sent_names = []

    for item in tags:
        raw_tag = item["raw_tag"]
        file_path = item["path"]
        caption = item["caption"]

        res = await send_media(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            thread_id=thread_id,
            media_type="auto",
            bot=bot,
        )
        sent_results.append(res)
        if res.get("status") == "success":
            cleaned_text = re.sub(
                r"([ \t]*)" + re.escape(raw_tag) + r"([ \t]*)",
                lambda m: " " if m.group(1) and m.group(2) else "",
                cleaned_text,
            )
            sent_names.append(Path(file_path).name)
        else:
            err_note = f"⚠️ [Falha ao enviar anexo '{Path(file_path).name}': {res.get('message', 'Erro')}]"
            cleaned_text = cleaned_text.replace(raw_tag, err_note)

    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
    if not cleaned_text and sent_names:
        files_str = ", ".join(f"`{name}`" for name in sent_names)
        cleaned_text = f"📎 Arquivo(s) enviado(s): {files_str}"

    return cleaned_text, sent_results


def cmd_send(args: argparse.Namespace) -> int:
    """CLI subcommand to send a file to a chat/topic."""
    try:
        res = asyncio.run(
            send_media(
                chat_id=args.chat_id,
                file_path=args.file,
                caption=args.caption,
                thread_id=args.thread_id,
                media_type=args.type,
            )
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res.get("status") == "success":
                print(f"✅ Arquivo enviado com sucesso ({res.get('type')}): {res.get('filename')}")
                if res.get("message_id"):
                    print(f"📌 Message ID: {res.get('message_id')}")
            else:
                print(f"❌ Erro ao enviar arquivo: {res.get('message', 'Erro desconhecido')}", file=sys.stderr)
        return 0 if res.get("status") == "success" else 1
    except Exception as e:
        err = {"status": "error", "message": str(e)}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"❌ Erro fatal ao enviar arquivo: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--json", action="store_true", help="Output in JSON format")

    parser = argparse.ArgumentParser(description="UziClaw Media Helper CLI", parents=[common_parent])
    subparsers = parser.add_subparsers(dest="command")

    p_send = subparsers.add_parser("send", parents=[common_parent], help="Send a file or media to Telegram chat")
    p_send.add_argument("--chat-id", required=True, help="Telegram Chat ID (int or @username)")
    p_send.add_argument(
        "--thread-id",
        "--topic-id",
        dest="thread_id",
        type=int,
        default=None,
        help="Target forum topic / thread ID",
    )
    p_send.add_argument("--file", required=True, help="Path to file to send")
    p_send.add_argument("--caption", type=str, default=None, help="Optional caption")
    p_send.add_argument(
        "--type",
        choices=["auto", "document", "photo", "audio", "video"],
        default="auto",
        help="Media type (auto detects based on file extension)",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # If invoked directly with flags (e.g. `python -m src.media_helper --file ...`), default to 'send'
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["send"] + argv

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "send":
        sys.exit(cmd_send(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
