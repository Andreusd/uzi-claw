"""Session state management and history retrieval."""

import os
import re
import fcntl
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from .config import (
    DEFAULT_WORKSPACE,
    AUTOPILOT_DEFAULT,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_EFFORT,
    AGY_HISTORY_FILE,
    AGY_PRESENCE_DIR,
    AGY_BRAIN_DIR,
)
from .antigravity_runner import AntigravityRunner
from .terminal_runner import TerminalRunner

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    chat_id: int
    conversation_id: Optional[str] = None
    cwd: str = DEFAULT_WORKSPACE
    autopilot: bool = AUTOPILOT_DEFAULT
    mode: Optional[str] = DEFAULT_MODE or None
    model: Optional[str] = DEFAULT_MODEL or None
    effort: Optional[str] = DEFAULT_EFFORT or None
    runner: AntigravityRunner = field(default_factory=AntigravityRunner)
    terminal_mode: bool = False
    terminal_runner: TerminalRunner = field(default_factory=TerminalRunner)
    recent_conversations: list[str] = field(default_factory=list)

    def reset_conversation(self) -> None:
        """Reset conversation to start a new chat turn on next message."""
        self.conversation_id = None
        self.terminal_mode = False


class SessionManager:
    """Manages user and chat sessions."""

    def __init__(self):
        self._sessions: Dict[int, SessionState] = {}

    def get_session(self, chat_id: int) -> SessionState:
        """Retrieve or create session state for a given chat ID."""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    def list_recent_conversations(self, limit: int = 8) -> list[dict]:
        """
        Parse brain transcripts and history.jsonl and return unique recent conversations
        ordered newest to oldest.
        """
        conv_map: dict[str, dict] = {}

        # 1. Scan AGY_BRAIN_DIR for actual conversation transcripts (CLI, Telegram, IDE)
        if AGY_BRAIN_DIR.exists() and AGY_BRAIN_DIR.is_dir():
            try:
                dirs = [d for d in AGY_BRAIN_DIR.iterdir() if d.is_dir()]
                dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)

                for cdir in dirs[:40]:
                    cid = cdir.name
                    tpath = cdir / ".system_generated" / "logs" / "transcript.jsonl"
                    if not tpath.exists():
                        continue

                    first_prompt = None
                    latest_prompt = None
                    latest_ts = int(tpath.stat().st_mtime * 1000)

                    try:
                        with tpath.open("r", encoding="utf-8") as tf:
                            for line in tf:
                                if '"USER_INPUT"' not in line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                    if entry.get("type") == "USER_INPUT":
                                        content = entry.get("content", "")
                                        prompt = self._clean_prompt_display(content)
                                        # Skip automated test or empty prompts
                                        if prompt and prompt != "Reply with the single word: PONG":
                                            if not first_prompt:
                                                first_prompt = prompt
                                            latest_prompt = prompt
                                            ca = entry.get("created_at")
                                            if ca:
                                                try:
                                                    dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                                                    latest_ts = max(latest_ts, int(dt.timestamp() * 1000))
                                                except Exception:
                                                    pass
                                except Exception:
                                    continue
                    except Exception:
                        continue

                    chosen_prompt = first_prompt or latest_prompt
                    if chosen_prompt:
                        preview = chosen_prompt.replace("\n", " ").strip()
                        if len(preview) > 60:
                            preview = preview[:57] + "..."
                        conv_map[cid] = {
                            "id": cid,
                            "timestamp": latest_ts,
                            "preview": preview or "<empty prompt>",
                            "workspace": "",
                        }
            except Exception as e:
                logger.error("Failed to scan brain directory: %s", e)

        # 2. Also parse history.jsonl for CLI REPL prompt history
        if AGY_HISTORY_FILE.exists():
            try:
                with AGY_HISTORY_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            cid = entry.get("conversationId")
                            if not cid:
                                continue

                            ts = entry.get("timestamp", 0)
                            display = entry.get("display", "").strip()
                            ws = entry.get("workspace", "")

                            # Skip internal or empty commands if a previous readable message exists
                            if cid in conv_map and display.startswith("/") and len(conv_map[cid]["preview"]) > len(display):
                                conv_map[cid]["timestamp"] = max(conv_map[cid]["timestamp"], ts)
                            else:
                                preview = display.replace("\n", " ").strip()
                                if len(preview) > 60:
                                    preview = preview[:57] + "..."
                                if cid in conv_map:
                                    conv_map[cid]["timestamp"] = max(conv_map[cid]["timestamp"], ts)
                                    if ws:
                                        conv_map[cid]["workspace"] = ws
                                    if not conv_map[cid]["preview"] or conv_map[cid]["preview"] == "<empty prompt>":
                                        conv_map[cid]["preview"] = preview
                                else:
                                    conv_map[cid] = {
                                        "id": cid,
                                        "timestamp": ts,
                                        "preview": preview or "<empty prompt>",
                                        "workspace": ws,
                                    }
                        except Exception:
                            continue
            except Exception as e:
                logger.error("Failed to read agy history file: %s", e)

        if not conv_map:
            return []

        sorted_convs = sorted(conv_map.values(), key=lambda x: x["timestamp"], reverse=True)
        for c in sorted_convs:
            ts = c["timestamp"]
            if ts:
                try:
                    dt = datetime.fromtimestamp(ts / 1000)
                    c["time_str"] = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    c["time_str"] = ""
            else:
                c["time_str"] = ""
            c["is_running"] = self.is_conversation_running(c["id"])

        return sorted_convs[:limit]

    @staticmethod
    def _clean_prompt_display(raw: str) -> str:
        """Extract and format user message text for conversation preview."""
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
        text = m.group(1).strip() if m else raw.strip()
        text = re.sub(r"^\[Context:.*?\]\s*", "", text, flags=re.DOTALL).strip()

        # Friendly preview for media attachments
        if "User's note:" in text:
            note_m = re.search(r"User's note:\s*(.+)", text)
            if note_m:
                return note_m.group(1).strip()
        if text.startswith("[Attached Voice Note:"):
            return "🎙️ Voice note"
        if text.startswith("[Attached Image:"):
            return "🖼️ Image"
        if text.startswith("[Attached Audio File:"):
            return "🎵 Audio file"
        if text.startswith("[Attached Document:"):
            return "📄 Document"

        return text

    def is_conversation_running(self, cid: str) -> bool:
        """
        Check if an Antigravity agent conversation is currently active/running.
        Checks both active in-bot turn runners and agy CLI presence locks.
        """
        if not cid:
            return False

        # 1. Check local bot sessions
        for session in self._sessions.values():
            if session.runner.is_running and session.conversation_id == cid:
                return True

        # 2. Check kernel file locks in agy presence directory
        lock_file = AGY_PRESENCE_DIR / f"{cid}.lock"
        if lock_file.exists():
            try:
                fd = os.open(lock_file, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (BlockingIOError, OSError):
                    return True
                finally:
                    os.close(fd)
            except Exception:
                pass

        return False


# Global session manager instance
session_manager = SessionManager()
