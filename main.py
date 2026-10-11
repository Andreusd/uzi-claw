"""Main entry point for Antigravity Telegram Bot."""

import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import TELEGRAM_TOKEN
from src.bot import create_application


def setup_logging() -> None:
    """Configure console logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Run the bot."""
    setup_logging()
    logger = logging.getLogger("main")

    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        logger.error(
            "TELEGRAM_TOKEN is not set in .env! Please set your bot token in %s/.env",
            PROJECT_ROOT,
        )
        sys.exit(1)

    logger.info("Starting Antigravity Telegram Bot...")
    app = create_application()
    app.run_polling()


if __name__ == "__main__":
    main()
