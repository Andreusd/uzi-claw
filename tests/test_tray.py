"""Unit tests for the system tray indicator component."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.tray import HAS_DBUS, ICONS_DIR, start_tray_in_background
from src.session_manager import SessionManager, SessionState


class TestTrayComponent(unittest.TestCase):
    """Test system tray applet functions and graceful fallback."""

    def test_icons_exist(self):
        """Verify generated tray icons exist in icons directory."""
        icons_path = Path(ICONS_DIR)
        self.assertTrue(icons_path.exists(), "icons directory should exist")
        self.assertTrue((icons_path / "uziclaw.png").exists(), "uziclaw.png missing")
        self.assertTrue((icons_path / "uziclaw-active.png").exists(), "uziclaw-active.png missing")
        self.assertTrue((icons_path / "telegram-bridge.png").exists(), "telegram-bridge.png missing")
        self.assertTrue((icons_path / "telegram-bridge-active.png").exists(), "telegram-bridge-active.png missing")

    def test_headless_fallback(self):
        """Verify start_tray_in_background falls back gracefully when display is missing."""
        mock_sm = MagicMock(spec=SessionManager)
        # Should not throw unhandled exceptions
        app = start_tray_in_background(session_manager=mock_sm)
        # In headless or test environment, app is either None or an instance
        self.assertTrue(app is None or hasattr(app, "run"))


if __name__ == "__main__":
    unittest.main()
