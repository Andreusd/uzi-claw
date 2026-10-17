"""Configuration settings for Antigravity Telegram Bot."""

from pathlib import Path
import os
import shutil
import sys
from typing import Optional
from dotenv import load_dotenv

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# CLI binary path: default to environment, or which agy, or OS-specific install paths
_custom_agy_path = os.getenv("AGY_BIN_PATH")
if _custom_agy_path and Path(_custom_agy_path).exists():
    AGY_BIN_PATH = _custom_agy_path
else:
    _which_agy = shutil.which("agy")
    if _which_agy:
        AGY_BIN_PATH = _which_agy
    elif sys.platform == "win32":
        _win_candidates = [
            Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "agy" / "bin" / "agy.exe",
            Path.home() / "AppData" / "Local" / "agy" / "agy.exe",
            Path.home() / "AppData" / "Local" / "Google" / "Antigravity" / "bin" / "agy.exe",
        ]
        AGY_BIN_PATH = next((str(p) for p in _win_candidates if p.exists()), str(_win_candidates[0]))
    else:
        _home_agy = Path.home() / ".local" / "bin" / "agy"
        AGY_BIN_PATH = str(_home_agy)

# Working directory
DEFAULT_WORKSPACE = os.getenv(
    "DEFAULT_WORKSPACE", str(Path.home())
)

# Autopilot default (--dangerously-skip-permissions)
AUTOPILOT_DEFAULT = os.getenv("AUTOPILOT_DEFAULT", "true").lower() in ("true", "1", "yes")

# Default agent execution options
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "").strip()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "").strip()
DEFAULT_EFFORT = os.getenv("DEFAULT_EFFORT", "").strip()

# Prompt context injection (prepends Telegram origin metadata to prompts)
INJECT_TELEGRAM_CONTEXT = os.getenv("INJECT_TELEGRAM_CONTEXT", "true").lower() in ("true", "1", "yes")

# Shell configuration for direct terminal execution
DEFAULT_SHELL = os.getenv("DEFAULT_SHELL", "powershell" if sys.platform == "win32" else "bash")

# System tray icon (StatusNotifierItem on Linux desktop, pystray on Windows)
ENABLE_TRAY_ICON = os.getenv("ENABLE_TRAY_ICON", os.getenv("ENABLE_TRAY", "true")).lower() in ("true", "1", "yes")

# Media directory for incoming photos and voice notes
MEDIA_DIR = PROJECT_ROOT / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Authorized users file path (strict whitelist)
AUTHORIZED_USERS_FILE = PROJECT_ROOT / "authorized_users.txt"

# Antigravity CLI history file (for /recent command)
AGY_HISTORY_FILE = Path.home() / ".gemini" / "antigravity-cli" / "history.jsonl"
AGY_PRESENCE_DIR = Path.home() / ".gemini" / "antigravity-cli" / "presence"
AGY_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"

# Scheduled tasks persistence database
SCHEDULES_DB_PATH = PROJECT_ROOT / "data" / "schedules.db"
SCHEDULE_CHECK_INTERVAL = int(os.getenv("SCHEDULE_CHECK_INTERVAL", "30"))
SESSIONS_DB_PATH = PROJECT_ROOT / "data" / "sessions.db"

# Forum Supergroup Chat ID for topic isolation
_forum_id_str = os.getenv("FORUM_GROUP_ID", "").strip()
try:
    FORUM_GROUP_ID: Optional[int] = int(_forum_id_str) if _forum_id_str else None
except ValueError:
    FORUM_GROUP_ID = None

