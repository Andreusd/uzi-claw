"""Session state management and history retrieval."""

import os
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
)
from .antigravity_runner import AntigravityRunner

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
    recent_conversations: list[str] = field(default_factory=list)

    def reset_conversation(self) -> None:
        """Reset conversation to start a new chat turn on next message."""
        self.conversation_id = None


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
        Parse history.jsonl and return unique recent conversations ordered newest to oldest.
        """
        if not AGY_HISTORY_FILE.exists():
            return []

        conv_map: dict[str, dict] = {}

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
                            preview = display.replace("\n", " ")
                            if len(preview) > 60:
                                preview = preview[:57] + "..."
                            conv_map[cid] = {
                                "id": cid,
                                "timestamp": ts,
                                "preview": preview or "<empty prompt>",
                                "workspace": ws,
                            }
                    except Exception:
                        continue

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
        except Exception as e:
            logger.error("Failed to read agy history file: %s", e)
            return []

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
