"""Native Linux system tray indicator (StatusNotifierItem + DBusMenu) for Antigravity Telegram Bridge."""

import os
import sys
import logging
import threading
import subprocess
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = str(PROJECT_ROOT / "icons")
ENV_FILE = PROJECT_ROOT / ".env"

# Ensure D-Bus and GLib are available
HAS_DBUS = False
try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    HAS_DBUS = True
except ImportError as e:
    logger.warning("D-Bus or GLib not available in environment: %s. System tray will be disabled.", e)


if HAS_DBUS:
    class StatusNotifierItem(dbus.service.Object):
        """Implements org.kde.StatusNotifierItem D-Bus interface."""

        def __init__(self, bus: dbus.Bus, object_path: str, menu_path: str):
            super().__init__(bus, object_path)
            self.menu_path = menu_path
            self.icon_name = "telegram-bridge"
            self.title = "Antigravity Bridge: Online"
            self.status = "Active"

        @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
        def Get(self, interface: str, prop: str):
            if prop == "Category":
                return "ApplicationStatus"
            elif prop == "Id":
                return "antigravity-telegram-bridge"
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
                "Id": "antigravity-telegram-bridge",
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

        @dbus.service.method("com.canonical.dbusmenu", in_signature="iias", out_signature="u(ia{sv}av)")
        def GetLayout(self, parent_id: int, recursion_depth: int, property_names: list):
            items = []
            self.callbacks.clear()
            item_id = 1

            empty_children = dbus.Array([], signature="v")
            empty_props = dbus.Dictionary({}, signature="sv")

            # 1. Header (Title)
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("✈️ Antigravity Telegram Bridge", variant_level=1),
                    "enabled": dbus.Boolean(False, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            # 2. Status details
            is_running, run_desc, session_info = self.app.get_activity_status()
            status_bullet = "⚡" if is_running else "🟢"
            status_text = f"{status_bullet} Status: {run_desc}"

            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String(f"    {status_text}", variant_level=1),
                    "enabled": dbus.Boolean(False, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            if session_info:
                if session_info.get("cid"):
                    cid_preview = session_info["cid"][:8] + "..." if len(session_info["cid"]) > 10 else session_info["cid"]
                    items.append((
                        dbus.Int32(item_id),
                        dbus.Dictionary({
                            "label": dbus.String(f"    💬 Chat: {cid_preview}", variant_level=1),
                            "enabled": dbus.Boolean(False, variant_level=1),
                        }, signature="sv"),
                        empty_children
                    ))
                    item_id += 1

                if session_info.get("cwd"):
                    cwd_name = Path(session_info["cwd"]).name or session_info["cwd"]
                    items.append((
                        dbus.Int32(item_id),
                        dbus.Dictionary({
                            "label": dbus.String(f"    📁 Dir: {cwd_name}", variant_level=1),
                            "enabled": dbus.Boolean(False, variant_level=1),
                        }, signature="sv"),
                        empty_children
                    ))
                    item_id += 1

                if "autopilot" in session_info:
                    ap_text = "Enabled (auto-run)" if session_info["autopilot"] else "Disabled (ask)"
                    items.append((
                        dbus.Int32(item_id),
                        dbus.Dictionary({
                            "label": dbus.String(f"    🤖 Autopilot: {ap_text}", variant_level=1),
                            "enabled": dbus.Boolean(False, variant_level=1),
                        }, signature="sv"),
                        empty_children
                    ))
                    item_id += 1

            # Separator
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({"type": dbus.String("separator", variant_level=1)}, signature="sv"),
                empty_children
            ))
            item_id += 1

            # 3. Actions: Interrupt (active task)
            if is_running:
                int_id = item_id
                self.callbacks[int_id] = self.app.interrupt_current_task
                items.append((
                    dbus.Int32(item_id),
                    dbus.Dictionary({
                        "label": dbus.String("🛑 Interrupt Running Task", variant_level=1),
                        "enabled": dbus.Boolean(True, variant_level=1),
                    }, signature="sv"),
                    empty_children
                ))
                item_id += 1

            # New session action
            new_id = item_id
            self.callbacks[new_id] = self.app.reset_current_session
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("🆕 Start Fresh Conversation", variant_level=1),
                    "enabled": dbus.Boolean(True, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            # Separator
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({"type": dbus.String("separator", variant_level=1)}, signature="sv"),
                empty_children
            ))
            item_id += 1

            # Logs action
            logs_id = item_id
            self.callbacks[logs_id] = open_terminal_logs
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("📋 View Live Logs (Terminal)", variant_level=1),
                    "enabled": dbus.Boolean(True, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            # Edit .env config action
            cfg_id = item_id
            self.callbacks[cfg_id] = open_config_file
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("⚙️ Edit Configuration (.env)", variant_level=1),
                    "enabled": dbus.Boolean(True, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            # Restart service action
            restart_id = item_id
            self.callbacks[restart_id] = self.app.restart_service
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("🔄 Restart Bridge Service", variant_level=1),
                    "enabled": dbus.Boolean(True, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

            # Stop service action
            stop_id = item_id
            self.callbacks[stop_id] = self.app.stop_service
            items.append((
                dbus.Int32(item_id),
                dbus.Dictionary({
                    "label": dbus.String("⏹️ Stop Bridge Service", variant_level=1),
                    "enabled": dbus.Boolean(True, variant_level=1),
                }, signature="sv"),
                empty_children
            ))
            item_id += 1

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
        """Controller for the D-Bus system tray indicator."""

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

            self.register_tray()
            self.refresh_state()

            # Poll every 2 seconds for state changes
            GLib.timeout_add_seconds(2, self.periodic_check)

        def register_tray(self):
            try:
                watcher = self.bus.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
                watcher.RegisterStatusNotifierItem("/StatusNotifierItem", dbus_interface="org.kde.StatusNotifierWatcher")
                logger.info("Registered Telegram Bridge tray with StatusNotifierWatcher")
            except Exception as e:
                logger.warning("Could not register with StatusNotifierWatcher: %s", e)

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
                icon = "telegram-bridge-active"
                title = f"Antigravity Bridge: {desc}"
            else:
                icon = "telegram-bridge"
                title = "Antigravity Bridge: Online"

            icon_changed = self.sni.update_icon(icon, title)
            if icon_changed or True:
                self.menu.notify_update()

        def periodic_check(self):
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
            logger.info("Tray clicked: Restarting antigravity-telegram.service")
            subprocess.Popen(["systemctl", "--user", "restart", "antigravity-telegram.service"])

        def stop_service(self):
            logger.info("Tray clicked: Stopping antigravity-telegram.service")
            subprocess.Popen(["systemctl", "--user", "stop", "antigravity-telegram.service"])

        def run(self):
            loop = GLib.MainLoop()
            loop.run()


def open_terminal_logs():
    """Launch terminal emulator following journalctl logs."""
    cmd = ["journalctl", "--user", "-u", "antigravity-telegram.service", "-f"]
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


def start_tray_in_background(session_manager=None) -> Optional["TelegramBridgeTrayApp"]:
    """Initialize and start the system tray icon in a dedicated daemon thread."""
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
        logger.info("Antigravity Telegram Bridge tray icon started in background thread.")
        return app
    except Exception as e:
        logger.warning("Could not initialize system tray indicator: %s", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from src.session_manager import session_manager as default_sm
    print("Starting standalone Telegram Bridge tray indicator...")
    tray = TelegramBridgeTrayApp(session_manager=default_sm)
    tray.run()
