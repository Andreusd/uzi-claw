"""Device and system statistics collector for Linux hosts."""

import glob
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def make_progress_bar(percent: float, length: int = 10) -> str:
    """Generate a Unicode progress bar like [███████░░░]."""
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100.0) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def format_bytes(bytes_val: float) -> str:
    """Convert bytes into human-readable string (MB, GB, TB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024.0 or unit == "TB":
            if unit in ("GB", "TB"):
                return f"{bytes_val:.1f} {unit}"
            return f"{int(bytes_val)} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} GB"


def get_battery_info() -> Optional[dict[str, Any]]:
    """
    Read battery status, capacity, and power drain from Windows API or /sys/class/power_supply.
    Returns None if no battery is found.
    """
    if platform.system() == "Windows":
        try:
            import ctypes
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            sps = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
                # BatteryFlag == 128 means No System Battery, 255 means Unknown
                if sps.BatteryFlag == 128 or sps.BatteryLifePercent == 255:
                    return None
                capacity = int(sps.BatteryLifePercent)
                ac_connected = (sps.ACLineStatus == 1)
                is_charging = bool(sps.BatteryFlag & 8)
                if is_charging:
                    status = "Charging"
                elif ac_connected:
                    status = "Full / AC Connected" if capacity >= 95 else "AC Connected"
                else:
                    status = "Discharging"

                time_str = ""
                if sps.BatteryLifeTime != 0xFFFFFFFF and sps.BatteryLifeTime > 0:
                    h, rem = divmod(sps.BatteryLifeTime, 3600)
                    m = rem // 60
                    time_str = f"~{h}h {m}m left"

                return {
                    "name": "BAT0",
                    "capacity": capacity,
                    "status": status,
                    "power_w": 0.0,
                    "time_estimate": time_str,
                    "ac_connected": ac_connected,
                }
        except Exception as e:
            logger.debug("Failed to read Windows battery: %s", e)

    bat_paths = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bat_paths:
        return None

    bat_dir = Path(bat_paths[0])

    def read_file(filename: str, default: str = "") -> str:
        f = bat_dir / filename
        try:
            return f.read_text(encoding="utf-8").strip() if f.is_file() else default
        except Exception:
            return default

    try:
        capacity = int(read_file("capacity", "0"))
    except ValueError:
        capacity = 0

    status = read_file("status", "Unknown")

    # Read power / energy or current / voltage
    try:
        power_uW = float(read_file("power_now", "0"))
    except ValueError:
        power_uW = 0.0

    try:
        energy_uWh = float(read_file("energy_now", "0"))
    except ValueError:
        energy_uWh = 0.0

    try:
        energy_full_uWh = float(read_file("energy_full", "0"))
    except ValueError:
        energy_full_uWh = 0.0

    # Fallback to current and voltage if power_now is not provided
    if power_uW == 0:
        try:
            current_uA = float(read_file("current_now", "0"))
            voltage_uV = float(read_file("voltage_now", "0"))
            power_uW = (current_uA * voltage_uV) / 1_000_000.0
        except ValueError:
            power_uW = 0.0

    # Fallback to charge and voltage if energy is not provided
    if energy_uWh == 0:
        try:
            charge_uAh = float(read_file("charge_now", "0"))
            voltage_uV = float(read_file("voltage_now", "0"))
            energy_uWh = (charge_uAh * voltage_uV) / 1_000_000.0
        except ValueError:
            energy_uWh = 0.0

    if energy_full_uWh == 0:
        try:
            charge_full_uAh = float(read_file("charge_full", "0"))
            voltage_uV = float(read_file("voltage_now", "0"))
            energy_full_uWh = (charge_full_uAh * voltage_uV) / 1_000_000.0
        except ValueError:
            energy_full_uWh = 0.0

    power_w = power_uW / 1_000_000.0
    energy_wh = energy_uWh / 1_000_000.0
    energy_full_wh = energy_full_uWh / 1_000_000.0

    time_str = ""
    status_lower = status.lower()
    if status_lower == "discharging" and power_w > 0.2:
        hours = energy_wh / power_w
        h = int(hours)
        m = int((hours - h) * 60)
        time_str = f"~{h}h {m}m left"
    elif status_lower == "charging" and power_w > 0.2 and energy_full_wh > energy_wh:
        hours = (energy_full_wh - energy_wh) / power_w
        h = int(hours)
        m = int((hours - h) * 60)
        time_str = f"~{h}h {m}m until full"

    # Check AC connection
    ac_connected = False
    for ac_path in glob.glob("/sys/class/power_supply/*"):
        p = Path(ac_path)
        if p.name.startswith("BAT"):
            continue
        try:
            online_file = p / "online"
            if online_file.exists() and online_file.read_text(encoding="utf-8").strip() == "1":
                ac_connected = True
                break
        except Exception:
            pass

    return {
        "name": bat_dir.name,
        "capacity": capacity,
        "status": status,
        "power_w": power_w,
        "time_estimate": time_str,
        "ac_connected": ac_connected,
    }


def get_cpu_info() -> dict[str, Any]:
    """Retrieve CPU load average, core count, and model."""
    cores = os.cpu_count() or 1
    model = ""
    load1, load5, load15 = 0.0, 0.0, 0.0

    if platform.system() == "Windows":
        model = platform.processor() or ""
    else:
        try:
            load1, load5, load15 = os.getloadavg()
        except (AttributeError, OSError):
            load1, load5, load15 = 0.0, 0.0, 0.0

        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            try:
                with cpuinfo.open("r", encoding="utf-8") as f:
                    for line in f:
                        if "model name" in line:
                            model = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass

    return {
        "load_1m": load1,
        "load_5m": load5,
        "load_15m": load15,
        "cores": cores,
        "model": model,
    }


def get_memory_info() -> dict[str, Any]:
    """Parse memory statistics for RAM and Swap."""
    if platform.system() == "Windows":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                tot = stat.ullTotalPhys
                avail = stat.ullAvailPhys
                used = max(0, tot - avail)
                mem_pct = float(stat.dwMemoryLoad)

                swap_tot = max(0, stat.ullTotalPageFile - stat.ullTotalPhys)
                swap_avail = max(0, stat.ullAvailPageFile - stat.ullAvailPhys)
                swap_used = max(0, swap_tot - swap_avail)
                swap_pct = (swap_used / swap_tot * 100.0) if swap_tot else 0.0

                return {
                    "total_bytes": tot,
                    "used_bytes": used,
                    "available_bytes": avail,
                    "used_percent": mem_pct,
                    "swap_total_bytes": swap_tot,
                    "swap_used_bytes": swap_used,
                    "swap_percent": swap_pct,
                }
        except Exception as e:
            logger.debug("Failed to read Windows memory: %s", e)

    mem_dict: dict[str, int] = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            with meminfo.open("r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_str = parts[1].split()[0]
                        mem_dict[key] = int(val_str)
        except Exception:
            pass

    tot_kb = mem_dict.get("MemTotal", 0)
    avail_kb = mem_dict.get("MemAvailable", mem_dict.get("MemFree", 0))
    used_kb = max(0, tot_kb - avail_kb)
    mem_pct = (used_kb / tot_kb * 100.0) if tot_kb else 0.0

    swap_tot_kb = mem_dict.get("SwapTotal", 0)
    swap_free_kb = mem_dict.get("SwapFree", 0)
    swap_used_kb = max(0, swap_tot_kb - swap_free_kb)
    swap_pct = (swap_used_kb / swap_tot_kb * 100.0) if swap_tot_kb else 0.0

    return {
        "total_bytes": tot_kb * 1024,
        "used_bytes": used_kb * 1024,
        "available_bytes": avail_kb * 1024,
        "used_percent": mem_pct,
        "swap_total_bytes": swap_tot_kb * 1024,
        "swap_used_bytes": swap_used_kb * 1024,
        "swap_percent": swap_pct,
    }


def get_disk_info(path: Optional[str] = None) -> dict[str, Any]:
    """Retrieve filesystem space usage."""
    if not path or path == "/":
        path = (os.environ.get("SystemDrive", "C:") + "\\") if platform.system() == "Windows" else "/"
    try:
        usage = shutil.disk_usage(path)
        pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": pct,
        }
    except Exception as e:
        logger.debug("Failed to read disk usage: %s", e)
        return {
            "path": path,
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "used_percent": 0.0,
        }


def get_system_uptime() -> str:
    """Format system uptime into human-readable string."""
    if platform.system() == "Windows":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_uint64
            total_sec = k32.GetTickCount64() / 1000.0
            days, rem = divmod(int(total_sec), 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            if days > 0:
                return f"{days}d {hours}h {mins}m"
            return f"{hours}h {mins}m"
        except Exception:
            return "Unknown"

    uptime_file = Path("/proc/uptime")
    if not uptime_file.exists():
        return "Unknown"

    try:
        with uptime_file.open("r", encoding="utf-8") as f:
            total_sec = float(f.readline().split()[0])
            days, rem = divmod(int(total_sec), 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            if days > 0:
                return f"{days}d {hours}h {mins}m"
            return f"{hours}h {mins}m"
    except Exception:
        return "Unknown"


def get_os_pretty_name() -> str:
    """Get operating system pretty name."""
    if platform.system() == "Windows":
        return f"Windows {platform.release()} ({platform.version()})"

    os_release = Path("/etc/os-release")
    if os_release.exists():
        try:
            with os_release.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip('"\n')
        except Exception:
            pass
    return f"{platform.system()} {platform.release()}"


def format_device_stats() -> str:
    """
    Format all system metrics (battery, CPU, memory, disk, uptime) into
    a clean, mobile-friendly Telegram Markdown message.
    """
    lines: list[str] = ["📊 **Device & System Stats**\n"]

    # 1. Battery
    bat = get_battery_info()
    if bat:
        cap = bat["capacity"]
        status = bat["status"]
        time_est = bat["time_estimate"]
        ac_conn = bat["ac_connected"]
        power_w = bat["power_w"]

        # Choose icon
        status_l = status.lower()
        if ac_conn and status_l == "charging":
            bat_icon = "⚡"
        elif ac_conn:
            bat_icon = "🔌"
        elif cap <= 20:
            bat_icon = "🪫"
        else:
            bat_icon = "🔋"

        time_part = f" ({time_est})" if time_est else ""
        power_part = f" • {power_w:.1f}W" if power_w > 0.1 else ""
        ac_part = " [AC Connected]" if ac_conn and status_l != "charging" else ""

        lines.append(f"{bat_icon} **Battery:** {cap}% — {status}{time_part}{ac_part}")
        lines.append(f"`[{make_progress_bar(cap)}]`{power_part}\n")
    else:
        # Check if AC adapter is online
        ac_online = False
        if platform.system() == "Windows":
            try:
                import ctypes
                class SPS(ctypes.Structure):
                    _fields_ = [("ACLineStatus", ctypes.c_ubyte)]
                s = SPS()
                if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
                    ac_online = (s.ACLineStatus == 1)
            except Exception:
                pass
        else:
            for ac_path in glob.glob("/sys/class/power_supply/*"):
                p = Path(ac_path)
                try:
                    on = p / "online"
                    if on.exists() and on.read_text(encoding="utf-8").strip() == "1":
                        ac_online = True
                        break
                except Exception:
                    pass
        pwr_text = "Connected (AC Power)" if ac_online else "AC Powered (No battery)"
        lines.append(f"🔌 **Power:** {pwr_text}\n")

    # 2. CPU
    cpu = get_cpu_info()
    c_cores = cpu["cores"]
    c_1m = cpu["load_1m"]
    c_5m = cpu["load_5m"]
    c_15m = cpu["load_15m"]
    c_model = cpu["model"]
    model_line = f" • `{c_model}`" if c_model else ""

    if platform.system() == "Windows":
        lines.append(f"⚙️ **CPU Load:** {c_cores} cores{model_line}")
    else:
        lines.append(f"⚙️ **CPU Load:** `{c_1m:.2f}`, `{c_5m:.2f}`, `{c_15m:.2f}` ({c_cores} cores){model_line}")

    # 3. RAM & Swap
    mem = get_memory_info()
    tot_mem = format_bytes(mem["total_bytes"])
    used_mem = format_bytes(mem["used_bytes"])
    mem_pct = mem["used_percent"]
    lines.append(f"🧠 **RAM:** {used_mem} / {tot_mem} ({mem_pct:.0f}%)")
    lines.append(f"`[{make_progress_bar(mem_pct)}]`")

    if mem["swap_total_bytes"] > 0:
        used_sw = format_bytes(mem["swap_used_bytes"])
        tot_sw = format_bytes(mem["swap_total_bytes"])
        sw_pct = mem["swap_percent"]
        lines.append(f"🔄 **Swap:** {used_sw} / {tot_sw} ({sw_pct:.0f}%)\n")
    else:
        lines.append("")

    # 4. Disk
    disk = get_disk_info()
    if disk["total_bytes"] > 0:
        d_used = format_bytes(disk["used_bytes"])
        d_tot = format_bytes(disk["total_bytes"])
        d_pct = disk["used_percent"]
        d_path = disk.get("path", "/")
        lines.append(f"💾 **Disk ({d_path}):** {d_used} / {d_tot} ({d_pct:.0f}%)")
        lines.append(f"`[{make_progress_bar(d_pct)}]`\n")

    # 5. OS & Host Info
    host = platform.node() or "Local Machine"
    os_name = get_os_pretty_name()
    uptime = get_system_uptime()

    lines.append(f"🖥️ **Host:** `{host}`")
    lines.append(f"• **OS:** {os_name}")
    lines.append(f"• **Uptime:** {uptime}")

    return "\n".join(lines)
