"""Session state management and history retrieval."""

import asyncio
import os
import re
import fcntl
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from .config import (
    DEFAULT_WORKSPACE,
    AUTOPILOT_DEFAULT,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_EFFORT,
    AGY_HISTORY_FILE,
    AGY_PRESENCE_DIR,
    AGY_BRAIN_DIR,
    SESSIONS_DB_PATH,
    FORUM_GROUP_ID,
)
from .antigravity_runner import AntigravityRunner
from .terminal_runner import TerminalRunner

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    chat_id: int
    thread_id: Optional[int] = None
    forum_topic_id: Optional[int] = None
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
    is_busy: bool = False
    cancel_requested: bool = False
    queued_count: int = 0
    queue_generation: int = 0
    background_runners: list[AntigravityRunner] = field(default_factory=list)
    _lock: Optional[asyncio.Lock] = field(default=None, repr=False)
    _loop: Optional[object] = field(default=None, repr=False)

    @property
    def lock(self) -> asyncio.Lock:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._lock is None or (current_loop is not None and self._loop is not current_loop):
            self._lock = asyncio.Lock()
            self._loop = current_loop
        return self._lock

    def clear_queue(self) -> None:
        """Invalidate currently queued messages."""
        self.cancel_requested = True
        self.queue_generation += 1

    def reset_conversation(self) -> None:
        """Reset conversation to start a new chat turn on next message."""
        self.conversation_id = None
        self.forum_topic_id = None
        self.terminal_mode = False
        self.cancel_requested = False
        self.queue_generation += 1

    def detach_running_turn(self) -> AntigravityRunner:
        """
        Detach the currently running turn into a background runner,
        allocating a brand new runner, lock, and fresh state for the session.
        Returns the detached runner so it can finish in background.
        """
        detached = self.runner
        if detached.is_running:
            self.background_runners.append(detached)

        # Allocate fresh runner & lock for the user's new conversation
        self.runner = AntigravityRunner(bin_path=self.runner.bin_path)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._lock = asyncio.Lock()
        self._loop = loop

        # Reset session properties for fresh conversation
        self.conversation_id = None
        self.forum_topic_id = None
        self.terminal_mode = False
        self.is_busy = False
        self.cancel_requested = False
        self.queued_count = 0
        self.queue_generation += 1
        return detached


class SessionManager:
    """Manages user and chat sessions with SQLite persistence and topic isolation."""

    def __init__(self, db_path: Path = SESSIONS_DB_PATH):
        self.db_path = db_path
        self._sessions: Dict[str, SessionState] = {}
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Create sessions table in SQLite if not existing, with migrations."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            try:
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS sessions (
                            session_key TEXT PRIMARY KEY,
                            chat_id INTEGER NOT NULL,
                            thread_id INTEGER,
                            conversation_id TEXT,
                            cwd TEXT NOT NULL,
                            autopilot INTEGER NOT NULL,
                            mode TEXT,
                            model TEXT,
                            effort TEXT,
                            updated_at REAL NOT NULL,
                            forum_topic_id INTEGER
                        );
                    """)
                    # Migration check in case table exists without forum_topic_id
                    cursor = conn.cursor()
                    cursor.execute("PRAGMA table_info(sessions)")
                    cols = [r[1] for r in cursor.fetchall()]
                    if "forum_topic_id" not in cols:
                        conn.execute("ALTER TABLE sessions ADD COLUMN forum_topic_id INTEGER;")
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to initialize sessions database: %s", e)

    def _session_key(self, chat_id: int, thread_id: Optional[int] = None) -> str:
        return f"{chat_id}:{thread_id or 0}"

    def save_session(self, session: SessionState) -> None:
        """Persist session state to SQLite."""
        try:
            key = self._session_key(session.chat_id, session.thread_id)
            conn = sqlite3.connect(self.db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            session_key, chat_id, thread_id, conversation_id,
                            cwd, autopilot, mode, model, effort, updated_at, forum_topic_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_key) DO UPDATE SET
                            conversation_id=excluded.conversation_id,
                            cwd=excluded.cwd,
                            autopilot=excluded.autopilot,
                            mode=excluded.mode,
                            model=excluded.model,
                            effort=excluded.effort,
                            updated_at=excluded.updated_at,
                            forum_topic_id=excluded.forum_topic_id
                        """,
                        (
                            key,
                            session.chat_id,
                            session.thread_id,
                            session.conversation_id,
                            session.cwd,
                            1 if session.autopilot else 0,
                            session.mode,
                            session.model,
                            session.effort,
                            time.time(),
                            int(session.forum_topic_id) if isinstance(session.forum_topic_id, (int, float)) else None,
                        ),
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to save session state to DB: %s", e)

    def _load_session(self, chat_id: int, thread_id: Optional[int] = None) -> Optional[SessionState]:
        """Load session state from SQLite if exists."""
        try:
            key = self._session_key(chat_id, thread_id)
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT conversation_id, cwd, autopilot, mode, model, effort, forum_topic_id FROM sessions WHERE session_key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if row:
                    return SessionState(
                        chat_id=chat_id,
                        thread_id=thread_id,
                        conversation_id=row[0],
                        cwd=row[1] or DEFAULT_WORKSPACE,
                        autopilot=bool(row[2]),
                        mode=row[3] or None,
                        model=row[4] or None,
                        effort=row[5] or None,
                        forum_topic_id=row[6] if len(row) > 6 else None,
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to load session state from DB: %s", e)
        return None

    def get_session(self, chat_id: int, thread_id: Optional[int] = None) -> SessionState:
        """Retrieve or create session state for a given chat ID and optional thread ID."""
        key = self._session_key(chat_id, thread_id)
        if key not in self._sessions:
            loaded = self._load_session(chat_id, thread_id)
            if loaded:
                self._sessions[key] = loaded
            else:
                self._sessions[key] = SessionState(chat_id=chat_id, thread_id=thread_id)
        return self._sessions[key]

    def find_topic_for_conversation(self, cid: str) -> Optional[int]:
        """Find existing forum topic ID for a given conversation ID, if one exists."""
        if not cid:
            return None

        # 1. Check active in-memory sessions
        for s in self._sessions.values():
            if s.conversation_id == cid:
                if s.forum_topic_id and s.forum_topic_id > 1:
                    return int(s.forum_topic_id)
                if s.thread_id and s.thread_id > 1:
                    return int(s.thread_id)

        # 2. Query SQLite DB
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                if FORUM_GROUP_ID:
                    cursor.execute(
                        "SELECT thread_id FROM sessions WHERE chat_id = ? AND conversation_id = ? AND thread_id IS NOT NULL AND thread_id > 1 ORDER BY updated_at DESC LIMIT 1",
                        (FORUM_GROUP_ID, cid),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        return int(row[0])

                cursor.execute(
                    "SELECT forum_topic_id FROM sessions WHERE conversation_id = ? AND forum_topic_id IS NOT NULL AND forum_topic_id > 1 ORDER BY updated_at DESC LIMIT 1",
                    (cid,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return int(row[0])
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to query topic for conversation %s: %s", cid, e)

        return None

    def get_conversation_first_prompt(self, cid: str) -> Optional[str]:
        """Retrieve the initial user prompt from transcript or history for topic naming."""
        if not cid:
            return None

        # Try transcript.jsonl
        tpath = AGY_BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if tpath.exists():
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
                                if prompt and prompt != "Reply with the single word: PONG":
                                    return prompt
                        except Exception:
                            continue
            except Exception as e:
                logger.debug("Failed to read first prompt from transcript for %s: %s", cid, e)

        # Fallback to history.jsonl
        if AGY_HISTORY_FILE.exists():
            try:
                with AGY_HISTORY_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        if cid not in line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("conversationId") == cid:
                                display = entry.get("display", "").strip()
                                if display and not display.startswith("/"):
                                    return display
                        except Exception:
                            continue
            except Exception as e:
                logger.debug("Failed to read first prompt from history for %s: %s", cid, e)

        return None

    def get_conversation_turns(self, cid: str, max_turns: int = 2) -> list[dict]:
        """
        Extract recent conversation turns from transcript.jsonl.
        Returns a list of dicts: {'user': str, 'assistant': str, 'tools': list[str]}
        """
        if not cid:
            return []

        tpath = AGY_BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if not tpath.exists():
            return []

        turns = []
        current_user = None
        current_assistant = None
        tools_used = []

        try:
            with tpath.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    t = entry.get("type")
                    if t == "USER_INPUT":
                        if current_user is not None or current_assistant is not None:
                            turns.append({
                                "user": current_user,
                                "assistant": current_assistant,
                                "tools": list(tools_used),
                            })
                        current_user = entry.get("content", "")
                        current_assistant = None
                        tools_used = []
                    elif t == "PLANNER_RESPONSE":
                        content = entry.get("content")
                        if content:
                            current_assistant = content
                        t_calls = entry.get("tool_calls")
                        if t_calls and isinstance(t_calls, list):
                            for tc in t_calls:
                                name = tc.get("name")
                                if name and name not in tools_used:
                                    tools_used.append(name)

            if current_user is not None or current_assistant is not None:
                turns.append({
                    "user": current_user,
                    "assistant": current_assistant,
                    "tools": list(tools_used),
                })
        except Exception as e:
            logger.error("Failed to parse transcript turns for %s: %s", cid, e)
            return []

        valid_turns = []
        for turn in turns:
            u = self._clean_prompt_display(turn["user"] or "")
            a = (turn["assistant"] or "").strip()
            if u or a:
                valid_turns.append({
                    "user": u,
                    "assistant": a,
                    "tools": turn["tools"],
                })

        return valid_turns[-max_turns:]

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
