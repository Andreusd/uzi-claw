"""Media management and Telegram file downloads with retries."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from telegram.error import TimedOut, RetryAfter, NetworkError

from .config import MEDIA_DIR

logger = logging.getLogger(__name__)


def cleanup_old_media(max_age_seconds: float = 86400.0) -> int:
    """Remove media files older than max_age_seconds (default 24h)."""
    if not MEDIA_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    try:
        for f in MEDIA_DIR.iterdir():
            if f.is_file():
                try:
                    if now - f.stat().st_mtime > max_age_seconds:
                        f.unlink()
                        removed += 1
                except Exception:
                    pass
    except Exception as e:
        logger.debug("Failed media cleanup: %s", e)
    if removed:
        logger.info("Cleaned up %d expired media file(s) from %s", removed, MEDIA_DIR)
    return removed


async def download_telegram_file_with_retry(
    bot,
    file_id: str,
    local_path: Path,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> Path:
    """
    Download a Telegram media file with retries, exponential backoff, and extended timeouts.
    Handles network blips, TimedOut, and RetryAfter exceptions.
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Downloading media file %s (attempt %d/%d)...", file_id, attempt, max_retries)
            file_obj = await bot.get_file(file_id, read_timeout=timeout, connect_timeout=15.0)
            await file_obj.download_to_drive(str(local_path), read_timeout=timeout, connect_timeout=15.0)
            logger.info("Successfully downloaded media to %s", local_path)
            return local_path
        except RetryAfter as e:
            last_err = e
            logger.warning("Telegram RetryAfter %ss on file download attempt %d/%d", e.retry_after, attempt, max_retries)
            await asyncio.sleep(e.retry_after)
        except (TimedOut, NetworkError, Exception) as e:
            last_err = e
            logger.warning("Media download attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                await asyncio.sleep(attempt * 1.5)

    if last_err:
        raise last_err
    raise RuntimeError("Media download failed after retries")
