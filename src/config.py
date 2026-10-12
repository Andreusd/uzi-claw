"""Configuration settings for Antigravity Telegram Bot."""

from pathlib import Path
import os
import shutil
from dotenv import load_dotenv

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# CLI binary path: default to environment, or ~/.local/bin/agy, or which agy
_custom_agy_path = os.getenv("AGY_BIN_PATH")
if _custom_agy_path and Path(_custom_agy_path).exists():
    AGY_BIN_PATH = _custom_agy_path
else:
    _which_agy = shutil.which("agy")
    if _which_agy:
        AGY_BIN_PATH = _which_agy
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

# System tray icon (StatusNotifierItem on Linux desktop)
ENABLE_TRAY_ICON = os.getenv("ENABLE_TRAY_ICON", "true").lower() in ("true", "1", "yes")

# Media directory for incoming photos and voice notes
MEDIA_DIR = PROJECT_ROOT / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Authorized users file path (strict whitelist)
AUTHORIZED_USERS_FILE = PROJECT_ROOT / "authorized_users.txt"

# Antigravity CLI history file (for /recent command)
AGY_HISTORY_FILE = Path.home() / ".gemini" / "antigravity-cli" / "history.jsonl"
AGY_PRESENCE_DIR = Path.home() / ".gemini" / "antigravity-cli" / "presence"
AGY_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
