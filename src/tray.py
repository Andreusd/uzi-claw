"""Native Linux system tray indicator (StatusNotifierItem + DBusMenu) for UziClaw Telegram Bridge."""

import os
import sys
import time
import logging
import threading
import subprocess
from pathlib import Path
from typing import Optional, Callable, Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = str(PROJECT_ROOT / "icons")
ENV_FILE = PROJECT_ROOT / ".env"

# Ensure D-Bus and GLib are available on Linux
HAS_DBUS = False
try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    HAS_DBUS = True
except ImportError as e:
    if sys.platform != "win32":
        logger.warning("D-Bus or GLib not available in environment: %s. System tray will be disabled.", e)

# Ensure pystray and Pillow are available on Windows
HAS_PYSTRAY = False
pystray = None
if sys.platform == "win32":
    try:
        import pystray
        HAS_PYSTRAY = True
    except Exception as e:
        logger.debug("pystray not available on Windows: %s", e)
        pystray = None

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None


if HAS_DBUS:
    class StatusNotifierItem(dbus.service.Object):
        """Implements org.kde.StatusNotifierItem D-Bus interface."""

        def __init__(self, bus: dbus.Bus, object_path: str, menu_path: str):
            super().__init__(bus, object_path)
            self.menu_path = menu_path
            self.icon_name = "uziclaw"
            self.title = "UziClaw: Online"
            self.status = "Active"

        @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
        def Get(self, interface: str, prop: str):
            if prop == "Category":
                return "ApplicationStatus"
            elif prop == "Id":
                return "uziclaw-bridge"
            elif prop == "Title":
                return self.title
            elif prop == "Status":
                return self.status
            elif prop == "IconName":
                return self.icon_name
            elif prop == "IconThemePath":
                return ICONS_DIR
            elif prop == "Menu":
                return dbus.ObjectPath(self.menu_path)
            elif prop == "ItemIsMenu":
                return False
            return ""

        @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface: str):
            return {
                "Category": "ApplicationStatus",
                "Id": "uziclaw-bridge",
                "Title": self.title,
                "Status": self.status,
                "IconName": self.icon_name,
                "IconThemePath": ICONS_DIR,
                "Menu": dbus.ObjectPath(self.menu_path),
                "ItemIsMenu": False,
            }

        @dbus.service.signal("org.kde.StatusNotifierItem")
        def NewIcon(self):
            pass

        @dbus.service.signal("org.kde.StatusNotifierItem")
        def NewStatus(self, status: str):
            pass

        @dbus.service.signal("org.kde.StatusNotifierItem")
        def NewTitle(self):
            pass

        @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii", out_signature="")
        def Activate(self, x: int = 0, y: int = 0):
            # Left click: open logs terminal
            open_terminal_logs()

        @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii", out_signature="")
        def SecondaryActivate(self, x: int = 0, y: int = 0):
            pass

        @dbus.service.method("org.kde.StatusNotifierItem", in_signature="is", out_signature="")
        def Scroll(self, delta: int, orientation: str):
            pass

        def update_icon(self, icon_name: str, title: str):
            changed = False
            if self.icon_name != icon_name:
                self.icon_name = icon_name
                self.NewIcon()
                changed = True
            if self.title != title:
                self.title = title
                self.NewTitle()
                changed = True
            return changed


    class DBusMenu(dbus.service.Object):
        """Implements com.canonical.dbusmenu D-Bus interface for top-panel dropdown."""

        def __init__(self, bus: dbus.Bus, object_path: str, app: "TelegramBridgeTrayApp"):
            super().__init__(bus, object_path)
            self.app = app
            self.revision = 1
            self.callbacks: dict[int, Callable[[], None]] = {}

        def _add_menu_item(self, items: list, item_id: int, label: str, enabled: bool = True, callback: Optional[Callable[[], None]] = None) -> int:
            empty_children = dbus.Array([], signature="v")
            if callback:
                self.callbacks[item_id] = callback
            
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String(label, variant_level=1),
                    "enabled": dbus.Boolean(enabled, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            return item_id + 1

        def _add_separator(self, items: list, item_id: int) -> int:
            empty_children = dbus.Array([], signature="v")
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({"type": dbus.String("separator", variant_level=1)}, signature="sv"),
                empty_children
            ))
            return item_id + 1

        def _build_header(self, items: list, item_id: int) -> int:
            return self._add_menu_item(items, item_id, "🐾 UziClaw Telegram Bridge", enabled=False)

        def _build_status_details(self, items: list, item_id: int, is_running: bool, run_desc: str, session_info: dict) -> int:
            status_bullet = "⚡" if is_running else "🟢"
            status_text = f"{status_bullet} Status: {run_desc}"
            item_id = self._add_menu_item(items, item_id, f"    {status_text}", enabled=False)

            if session_info:
                if session_info.get("cid"):
                    cid_preview = session_info["cid"][:8] + "..." if len(session_info["cid"]) > 10 else session_info["cid"]
                    item_id = self._add_menu_item(items, item_id, f"    💬 Chat: {cid_preview}", enabled=False)

                if session_info.get("cwd"):
                    cwd_name = Path(session_info["cwd"]).name or session_info["cwd"]
                    item_id = self._add_menu_item(items, item_id, f"    📁 Dir: {cwd_name}", enabled=False)

                if "autopilot" in session_info:
                    ap_text = "Enabled (auto-run)" if session_info["autopilot"] else "Disabled (ask)"
                    item_id = self._add_menu_item(items, item_id, f"    🤖 Autopilot: {ap_text}", enabled=False)

            return item_id

        def _build_actions(self, items: list, item_id: int, is_running: bool) -> int:
            if is_running:
                item_id = self._add_menu_item(items, item_id, "🛑 Interrupt Running Task", callback=self.app.interrupt_current_task)

            item_id = self._add_menu_item(items, item_id, "🆕 Start Fresh Conversation", callback=self.app.reset_current_session)
            item_id = self._add_separator(items, item_id)
            item_id = self._add_menu_item(items, item_id, "📋 View Live Logs (Terminal)", callback=open_terminal_logs)
            item_id = self._add_menu_item(items, item_id, "⚙️ Edit Configuration (.env)", callback=open_config_file)
            item_id = self._add_menu_item(items, item_id, "🔄 Restart Bridge Service", callback=self.app.restart_service)
            item_id = self._add_menu_item(items, item_id, "⏹️ Stop Bridge Service", callback=self.app.stop_service)
            
            return item_id

        @dbus.service.method("com.canonical.dbusmenu", in_signature="iias", out_signature="u(ia{sv}av)")
        def GetLayout(self, parent_id: int, recursion_depth: int, property_names: list):
            items = []
            self.callbacks.clear()
            item_id = 1

            empty_props = dbus.Dictionary({}, signature="sv")

            # 1. Header (Title)
            item_id = self._build_header(items, item_id)

            # 2. Status details
            is_running, run_desc, session_info = self.app.get_activity_status()
            item_id = self._build_status_details(items, item_id, is_running, run_desc, session_info)

            # Separator
            item_id = self._add_separator(items, item_id)

            # 3. Actions
            item_id = self._build_actions(items, item_id, is_running)

            root = (dbus.Int32(0), empty_props, dbus.Array(items, signature="(ia{sv}av)"))
            return (dbus.UInt32(self.revision), root)

        @dbus.service.method("com.canonical.dbusmenu", in_signature="aias", out_signature="a(ia{sv})")
        def GetGroupProperties(self, ids: list, property_names: list):
            return []

        @dbus.service.method("com.canonical.dbusmenu", in_signature="is", out_signature="v")
        def GetProperty(self, id: int, name: str):
            return ""

        @dbus.service.method("com.canonical.dbusmenu", in_signature="isvu", out_signature="")
        def Event(self, id: int, event_id: str, data: str, timestamp: int):
            if event_id == "clicked":
                cb = self.callbacks.get(int(id))
                if cb:
                    cb()

        @dbus.service.signal("com.canonical.dbusmenu", signature="ui")
        def LayoutUpdated(self, revision: int, parent: int):
            pass

        def notify_update(self):
            self.revision += 1
            self.LayoutUpdated(self.revision, 0)


    class TelegramBridgeTrayApp:
        """Controller for the D-Bus system tray indicator with adaptive polling and screensaver awareness."""

        def __init__(self, session_manager=None):
            self.session_manager = session_manager
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self.bus = dbus.SessionBus()

            self.menu = DBusMenu(self.bus, "/MenuBar", self)
            self.sni = StatusNotifierItem(self.bus, "/StatusNotifierItem", "/MenuBar")

            pid = os.getpid()
            self.bus_name = dbus.service.BusName(
                f"org.kde.StatusNotifierItem-{pid}-1",
                self.bus,
            )

            self.is_registered = False
            self._last_register_attempt = 0.0
            self._timer_id = None
            self._is_screen_locked = False

            self._setup_screensaver_listener()
            self.register_tray()
            self.refresh_state()

            # Schedule adaptive polling
            self._schedule_next(immediate=False)

        def _setup_screensaver_listener(self):
            """Listen to screensaver signals to sleep while screen is locked and wake immediately on unlock."""
            try:
                for iface in ("org.gnome.ScreenSaver", "org.freedesktop.ScreenSaver"):
                    try:
                        self.bus.add_signal_receiver(
                            self._on_screensaver_active_changed,
                            signal_name="ActiveChanged",
                            dbus_interface=iface,
                        )
                    except Exception:
                        pass
                self._is_screen_locked = self._check_screen_locked()
            except Exception as e:
                logger.debug("Could not setup screensaver listener: %s", e)

        def _check_screen_locked(self) -> bool:
            """Check if the screen is currently locked/asleep."""
            for iface in ("org.gnome.ScreenSaver", "org.freedesktop.ScreenSaver"):
                try:
                    obj = self.bus.get_object(iface, f"/{iface.replace('.', '/')}")
                    return bool(obj.GetActive(dbus_interface=iface))
                except Exception:
                    continue
            return False

        def _on_screensaver_active_changed(self, active: bool):
            """D-Bus callback when screen lock state toggles."""
            self._is_screen_locked = bool(active)
            logger.debug("Screensaver ActiveChanged: %s", self._is_screen_locked)
            if not self._is_screen_locked:
                self.refresh_state()
            self._schedule_next(immediate=False)

        def trigger_update(self):
            """Thread-safe trigger to refresh state immediately from external worker threads."""
            try:
                GLib.idle_add(self._on_triggered_update)
            except Exception:
                pass

        def _on_triggered_update(self):
            self.refresh_state()
            self._schedule_next(immediate=False)
            return False

        def _schedule_next(self, immediate: bool = False):
            """Schedule next check with adaptive interval."""
            if self._timer_id is not None:
                try:
                    GLib.source_remove(self._timer_id)
                except Exception:
                    pass
                self._timer_id = None

            if immediate:
                self._timer_id = GLib.idle_add(self._on_timer_tick)
                return

            if self._is_screen_locked or self._check_screen_locked():
                self._is_screen_locked = True
                interval = 30  # Screen is locked/off: poll very lazily
            else:
                self._is_screen_locked = False
                is_running, _, _ = self.get_activity_status()
                interval = 2 if is_running else 10  # 2s if task running, 10s if idle

            self._timer_id = GLib.timeout_add_seconds(interval, self._on_timer_tick)

        def _on_timer_tick(self):
            self.periodic_check()
            self._schedule_next(immediate=False)
            return False

        def register_tray(self):
            self._last_register_attempt = time.time()
            bus_service_name = self.bus_name.get_name()
            for watcher_name in ["org.kde.StatusNotifierWatcher", "org.ayatana.StatusNotifierWatcher"]:
                try:
                    watcher = self.bus.get_object(watcher_name, "/StatusNotifierWatcher")
                    # Pass canonical service name (org.kde.StatusNotifierItem-{pid}-1)
                    # to ensure the AppIndicator extension matches the busAnalyzer ID
                    # and avoids creating duplicate tray icons.
                    watcher.RegisterStatusNotifierItem(bus_service_name, dbus_interface=watcher_name)
                    self.is_registered = True
                    logger.info("Registered Telegram Bridge tray with %s as %s", watcher_name, bus_service_name)
                    return
                except Exception as e:
                    logger.debug("Could not register with %s: %s", watcher_name, e)
            self.is_registered = False
            logger.debug("Could not register with StatusNotifierWatcher (will retry or rely on bus scanner).")

        def get_activity_status(self) -> tuple[bool, str, dict]:
            """Return (is_running, description, session_details)."""
            if not self.session_manager:
                return False, "Online (Standby)", {}

            sessions = getattr(self.session_manager, "_sessions", {})
            for session in sessions.values():
                if session.terminal_runner and session.terminal_runner.is_running:
                    return True, "Running Shell Command", {
                        "cid": session.conversation_id or "Shell",
                        "cwd": session.cwd,
                        "autopilot": session.autopilot,
                    }
                if session.runner and session.runner.is_running:
                    return True, "Processing Agent Turn...", {
                        "cid": session.conversation_id or "New Turn",
                        "cwd": session.cwd,
                        "autopilot": session.autopilot,
                    }

            # If sessions exist, return last active info
            if sessions:
                last_session = list(sessions.values())[-1]
                return False, "Online (Idle)", {
                    "cid": last_session.conversation_id,
                    "cwd": last_session.cwd,
                    "autopilot": last_session.autopilot,
                }

            return False, "Online (Idle)", {}

        def refresh_state(self):
            is_running, desc, _ = self.get_activity_status()
            if is_running:
                icon = "uziclaw-active"
                title = f"UziClaw: {desc}"
            else:
                icon = "uziclaw"
                title = "UziClaw: Online"

            icon_changed = self.sni.update_icon(icon, title)
            if icon_changed:
                self.menu.notify_update()

        def periodic_check(self):
            now = time.time()
            if not self.is_registered and (now - self._last_register_attempt > 60.0):
                self.register_tray()

            if not self._is_screen_locked:
                self.refresh_state()
            return True

        def interrupt_current_task(self):
            """Interrupt active runner task if running."""
            logger.info("Tray clicked: Interrupt current task")
            if not self.session_manager:
                return
            sessions = getattr(self.session_manager, "_sessions", {})
            for session in sessions.values():
                if session.terminal_runner and session.terminal_runner.is_running:
                    session.terminal_runner.interrupt()
                if session.runner and session.runner.is_running:
                    # Async interrupt wrapper
                    try:
                        import asyncio
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.run_coroutine_threadsafe(session.runner.interrupt(), loop)
                    except Exception as e:
                        logger.error("Failed to interrupt runner: %s", e)
            self.refresh_state()

        def reset_current_session(self):
            """Reset conversation for active sessions."""
            logger.info("Tray clicked: Reset current session")
            if not self.session_manager:
                return
            sessions = getattr(self.session_manager, "_sessions", {})
            for session in sessions.values():
                session.reset_conversation()
            self.refresh_state()

        def restart_service(self):
            logger.info("Tray clicked: Restarting uziclaw.service")
            subprocess.Popen(["systemctl", "--user", "restart", "uziclaw.service"])

        def stop_service(self):
            logger.info("Tray clicked: Stopping uziclaw.service")
            subprocess.Popen(["systemctl", "--user", "stop", "uziclaw.service"])

        def stop(self):
            if self._timer_id is not None:
                try:
                    GLib.source_remove(self._timer_id)
                except Exception:
                    pass
                self._timer_id = None
            if hasattr(self, "loop") and self.loop and self.loop.is_running():
                try:
                    self.loop.quit()
                except Exception:
                    pass

        def run(self):
            self.loop = GLib.MainLoop()
            self.loop.run()


def open_terminal_logs():
    """Launch terminal emulator following journalctl logs."""
    cmd = ["journalctl", "--user", "-u", "uziclaw.service", "-f"]
    # Check preferred terminals
    for term in ["ptyxis", "gnome-terminal", "xterm"]:
        if subprocess.run(["which", term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            if term == "ptyxis":
                subprocess.Popen([term, "--"] + cmd)
            elif term == "gnome-terminal":
                subprocess.Popen([term, "--"] + cmd)
            else:
                subprocess.Popen([term, "-e", " ".join(cmd)])
            return
    # Fallback to direct background execution
    subprocess.Popen(cmd)


def open_config_file():
    """Open .env in default desktop editor."""
    if ENV_FILE.exists():
        subprocess.Popen(["xdg-open", str(ENV_FILE)])


def generate_tray_icon(size: int = 64) -> Optional[Any]:
    """Generate or load an icon image for the Windows system tray."""
    if not Image:
        return None

    crab_path = PROJECT_ROOT / "icons" / "uziclaw.png"
    if crab_path.exists():
        try:
            img = Image.open(crab_path).convert("RGBA")
            return img.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            pass

    if not ImageDraw:
        return None

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=(235, 71, 26))
    points = [(size // 2, 12), (size - 16, size - 14), (16, size - 14)]
    draw.polygon(points, fill=(255, 255, 255))
    inner_points = [(size // 2, 26), (size - 24, size - 18), (24, size - 18)]
    draw.polygon(inner_points, fill=(235, 71, 26))
    draw.ellipse((size - 18, size - 18, size - 2, size - 2), fill=(0, 230, 118), outline=(255, 255, 255), width=2)
    return img


class WindowsTrayManager:
    """Manages the Windows system tray icon and menus using pystray."""

    def __init__(self, session_manager=None):
        self.session_manager = session_manager
        self._icon: Optional["pystray.Icon"] = None
        self._is_running = False

    @property
    def is_available(self) -> bool:
        return HAS_PYSTRAY and pystray is not None

    def start(self) -> bool:
        """Start the system tray icon detached on its own thread."""
        if not self.is_available:
            logger.info("Windows system tray is not available (pystray or Pillow not installed).")
            return False

        if self._is_running:
            return True

        from .config import DEFAULT_WORKSPACE, AGY_BRAIN_DIR

        image = generate_tray_icon(64)
        if not image:
            return False

        def open_path(path_target: Path):
            def handler(icon, item):
                try:
                    if sys.platform == "win32":
                        os.startfile(str(path_target))
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(path_target)])
                    else:
                        subprocess.Popen(["xdg-open", str(path_target)])
                except Exception as e:
                    logger.error("Failed to open path %s: %s", path_target, e)
            return handler

        def on_exit(icon, item):
            logger.info("Exit requested from Windows system tray.")
            self.stop()
            import _thread
            _thread.interrupt_main()

        menu_items = [
            pystray.MenuItem("🦀 UziClaw: Active", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("📁 Open Working Folder", open_path(Path(DEFAULT_WORKSPACE))),
            pystray.MenuItem("📂 Open Project Folder", open_path(PROJECT_ROOT)),
            pystray.MenuItem("🧠 Open Antigravity Brain", open_path(AGY_BRAIN_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Quit & Stop UziClaw", on_exit),
        ]

        try:
            self._icon = pystray.Icon(
                name="UziClaw",
                icon=image,
                title="UziClaw Telegram Bridge (Running)",
                menu=pystray.Menu(*menu_items),
            )
            self._icon.run_detached()
            self._is_running = True
            logger.info("UziClaw Windows system tray icon started successfully.")
            return True
        except Exception as e:
            logger.error("Failed to launch Windows system tray icon: %s", e)
            return False

    def update_tooltip(self, text: str) -> None:
        """Update the hover tooltip text on the tray icon."""
        if self._icon and self._is_running:
            try:
                self._icon.title = f"UziClaw: {text}"
            except Exception:
                pass

    def trigger_update(self) -> None:
        """Trigger update on Windows tray if needed."""
        pass

    def stop(self) -> None:
        """Stop and remove the tray icon."""
        if self._icon and self._is_running:
            try:
                self._icon.stop()
            except Exception:
                pass
            finally:
                self._icon = None
                self._is_running = False
                logger.info("Windows system tray icon stopped.")


# Alias for compatibility with earlier Windows branch
SystemTrayManager = WindowsTrayManager
tray_manager = WindowsTrayManager()

_active_tray_instance = None


def notify_tray_state_changed() -> None:
    """Thread-safe trigger to immediately refresh the tray icon and state."""
    global _active_tray_instance
    if _active_tray_instance and hasattr(_active_tray_instance, "trigger_update"):
        _active_tray_instance.trigger_update()


def start_tray_in_background(session_manager=None):
    """Initialize and start the cross-platform system tray icon in background."""
    global _active_tray_instance

    if sys.platform == "win32":
        if not HAS_PYSTRAY:
            logger.info("Windows system tray disabled (pystray/Pillow not available).")
            return None
        tray = WindowsTrayManager(session_manager=session_manager)
        if tray.start():
            _active_tray_instance = tray
            return tray
        return None

    # Linux desktop with D-Bus
    if not HAS_DBUS:
        logger.info("System tray disabled (D-Bus/GLib not available).")
        return None

    # Check if graphical session / D-Bus session bus is available
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        logger.info("No graphical session or session bus detected; skipping system tray.")
        return None

    try:
        app = TelegramBridgeTrayApp(session_manager=session_manager)
        thread = threading.Thread(target=app.run, name="TelegramBridgeTray", daemon=True)
        thread.start()
        logger.info("UziClaw Telegram Bridge tray icon started in background thread.")
        _active_tray_instance = app
        return app
    except Exception as e:
        logger.warning("Could not initialize system tray indicator: %s", e)
        return None


def stop_tray() -> None:
    """Stop active system tray instance if running."""
    global _active_tray_instance
    if _active_tray_instance:
        try:
            if hasattr(_active_tray_instance, "stop"):
                _active_tray_instance.stop()
        except Exception as e:
            logger.debug("Failed to stop system tray cleanly: %s", e)
        finally:
            _active_tray_instance = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from src.session_manager import session_manager as default_sm
    print("Starting standalone Telegram Bridge tray indicator...")
    if sys.platform == "win32":
        tray = WindowsTrayManager(session_manager=default_sm)
        tray.start()
        import time
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            tray.stop()
    else:
        tray = TelegramBridgeTrayApp(session_manager=default_sm)
        tray.run()
