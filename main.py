"""Main entry point for UziClaw Telegram Bot."""

import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import TELEGRAM_TOKEN, ENABLE_TRAY_ICON
from src.session_manager import session_manager
from src.tray import start_tray_in_background, stop_tray
from src.bot import create_application


def setup_logging() -> None:
    """Configure console and file logging."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "bot.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main() -> None:
    """Run the bot."""
    setup_logging()
    logger = logging.getLogger("main")

    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        err_msg = (
            f"TELEGRAM_TOKEN is not configured!\n\n"
            f"Please create:\n{PROJECT_ROOT / '.env'}\n\n"
            f"and set your Telegram Bot token from @BotFather before starting."
        )
        logger.error(err_msg)
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, err_msg, "UziClaw Telegram Bridge", 0x10 | 0x0)
            except Exception:
                pass
        sys.exit(1)

    # Initialize desktop system tray icon if enabled
    if ENABLE_TRAY_ICON and "--no-tray" not in sys.argv:
        start_tray_in_background(session_manager=session_manager)

    logger.info("Starting UziClaw Telegram Bot...")
    try:
        app = create_application()
        app.run_polling()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested. Stopping bot...")
    finally:
        stop_tray()


if __name__ == "__main__":
    main()
