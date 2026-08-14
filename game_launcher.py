print("STARTING...", flush=True)
_BOOTSTRAP_STARTED_AT = __import__("time").perf_counter()
import base64
import copy
import ctypes
from ctypes import wintypes
from collections import deque
from datetime import datetime
from functools import lru_cache
import glob
import hashlib
import hmac
import json
import logging
import ntpath
import os
import platform
import queue
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
except ImportError as exc:
    startup_message = (
        "XVVIIX Launcher needs Python's Tk/Tcl component (tkinter).\n\n"
        "Re-run the official Python installer, choose Modify, and enable tcl/tk and IDLE.\n\n"
        f"Details: {exc}"
    )
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, startup_message, "XVVIIX Launcher — Missing Tk", 0x10)
        except (AttributeError, OSError):
            print(startup_message, file=sys.stderr)
    else:
        print(startup_message, file=sys.stderr)
    raise SystemExit(1) from exc

# Optional integrations must never prevent the launcher from opening.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except (ImportError, OSError):
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageTk
    HAS_PIL = True
except (ImportError, OSError):
    Image = None
    ImageDraw = None
    ImageEnhance = None
    ImageFilter = None
    ImageOps = None
    ImageTk = None
    HAS_PIL = False

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except (ImportError, OSError):
    InvalidTag = None
    AESGCM = None
    HAS_CRYPTOGRAPHY = False

try:
    import psutil
    HAS_PSUTIL = True
except (ImportError, OSError):
    psutil = None
    HAS_PSUTIL = False

# NVIDIA bindings are imported only if the Monitor feature is requested.
pynvml = None
_monitor_pynvml_attempted = False

HAS_HARDWARE_MONITOR = HAS_PSUTIL

try:
    import winsound
    HAS_WINSOUND = True
except (ImportError, OSError):
    winsound = None
    HAS_WINSOUND = False

try:
    import winreg
    HAS_WINREG = True
except (ImportError, OSError):
    winreg = None
    HAS_WINREG = False

# pygame supplies independent, volume-controlled background playback. It is
# deliberately separate from splash rendering so a failed audio device cannot
# prevent the main window from opening.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
pygame = None
HAS_PYGAME = True
_pygame_import_attempted = False


def load_optional_pygame():
    """Defer the comparatively expensive audio import until after first paint."""
    global pygame, HAS_PYGAME, _pygame_import_attempted
    if _pygame_import_attempted:
        return pygame
    _pygame_import_attempted = True
    try:
        pygame = __import__("pygame")
        HAS_PYGAME = True
    except (ImportError, OSError):
        pygame = None
        HAS_PYGAME = False
    return pygame

try:
    from icoextract import IconExtractor
    HAS_ICOEXTRACT = HAS_PIL
except (ImportError, OSError):
    IconExtractor = None
    HAS_ICOEXTRACT = False

try:
    import pythoncom
    import win32com.client as win32_client
    HAS_WIN32COM = True
except (ImportError, OSError):
    pythoncom = None
    win32_client = None
    HAS_WIN32COM = False

LOG = logging.getLogger("xvviix_launcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ==========================================
# INTEGRATED HARDWARE MONITOR BACKEND
# ==========================================
# This service is intentionally defined in the launcher: it owns no separate
# application window and feeds only the built-in Monitor tab and overlay.
MONITOR_IS_WINDOWS = os.name == "nt"
MONITOR_PROCESS_SAFETY_LIMIT = 512


def _load_monitor_pynvml():
    """Load optional NVIDIA telemetry only when Hardware Monitor is opened."""
    global pynvml, _monitor_pynvml_attempted
    if _monitor_pynvml_attempted:
        return pynvml
    _monitor_pynvml_attempted = True
    try:
        pynvml = __import__("pynvml")
    except (ImportError, OSError):
        pynvml = None
    return pynvml


def monitor_clamp_percent(value: Any) -> float | None:
    """Return a finite percentage in the inclusive 0..100 range."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(100.0, number))


def monitor_normalize_process_cpu(value: Any, logical_cpus: Any) -> float | None:
    """Convert psutil's multi-core process value to a Task-Manager-style percentage."""
    try:
        core_count = max(1, int(logical_cpus))
        normalized = float(value) / core_count
    except (TypeError, ValueError, OverflowError):
        return None
    return monitor_clamp_percent(normalized)


def monitor_format_bytes(value: Any) -> str:
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return "--"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    precision = 0 if index == 0 else (1 if amount < 100 else 0)
    return f"{amount:.{precision}f} {units[index]}"


def monitor_format_rate(value: Any) -> str:
    text = monitor_format_bytes(value)
    return "--" if text == "--" else f"{text}/s"


def _monitor_cpu_model_name() -> str:
    name = (platform.processor() or "").strip()
    if name:
        return name
    if _monitor_sys_platform_linux():
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.casefold().startswith("model name") and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.machine() or "Unknown processor"


def _monitor_sys_platform_linux() -> bool:
    return platform.system().casefold() == "linux"


class _MonitorLatencyProbe:
    """Measure TCP connection latency without blocking telemetry or Tk."""

    def __init__(self, host="1.1.1.1", port=443, interval=3.0, timeout=0.8):
        self.host = host
        self.port = int(port)
        self.interval = max(1.0, float(interval))
        self.timeout = max(0.2, min(2.0, float(timeout)))
        self._latest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hardware-latency")
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.2)

    def value(self):
        with self._lock:
            return self._latest

    def _measure(self):
        started = time.perf_counter()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return max(0.0, (time.perf_counter() - started) * 1000.0)
        except OSError:
            return None

    def _run(self):
        while not self._stop.is_set():
            measured = self._measure()
            with self._lock:
                self._latest = measured
            self._stop.wait(self.interval)


class _MonitorWindowsGpuEngineReader:
    """Read Windows GPU engine utilization through PDH when NVML is absent."""

    PDH_FMT_DOUBLE = 0x00000200

    def __init__(self):
        self.available = False
        self._ctypes = None
        self._pdh = None
        self._query = None
        self._counters = []
        self._value_struct = None
        if not MONITOR_IS_WINDOWS:
            return
        try:
            import ctypes

            class PdhCounterValue(ctypes.Structure):
                _fields_ = [("status", ctypes.c_ulong), ("value", ctypes.c_double)]

            self._ctypes = ctypes
            self._value_struct = PdhCounterValue
            self._pdh = ctypes.WinDLL("pdh.dll")
            self._query = ctypes.c_void_p()
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            self._add_counters()
            if self._counters:
                self._pdh.PdhCollectQueryData(self._query)
                self.available = True
        except (AttributeError, OSError, TypeError):
            self.available = False

    @staticmethod
    def _engine_type(path):
        marker = "engtype_"
        lowered = path.casefold()
        if marker not in lowered:
            return "unknown"
        return lowered.split(marker, 1)[1].split("_", 1)[0].split(")", 1)[0]

    def _add_counters(self):
        ctypes = self._ctypes
        path = r"\GPU Engine(*)\Utilization Percentage"
        length = ctypes.c_ulong(0)
        self._pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(length), 0)
        if not length.value:
            return
        buffer = (ctypes.c_wchar * length.value)()
        if self._pdh.PdhExpandWildCardPathW(None, path, buffer, ctypes.byref(length), 0) != 0:
            return
        for instance_path in filter(None, ctypes.wstring_at(buffer, length.value).split("\x00")):
            counter = ctypes.c_void_p()
            if self._pdh.PdhAddEnglishCounterW(
                self._query, instance_path, 0, ctypes.byref(counter)
            ) == 0:
                self._counters.append((self._engine_type(instance_path), counter))

    def sample(self):
        if not self.available:
            return None
        try:
            self._pdh.PdhCollectQueryData(self._query)
            totals = {}
            for engine, counter in self._counters:
                value = self._value_struct()
                result = self._pdh.PdhGetFormattedCounterValue(
                    counter, self.PDH_FMT_DOUBLE, None, self._ctypes.byref(value)
                )
                if result == 0 and value.value > 0:
                    totals[engine] = totals.get(engine, 0.0) + value.value
            return monitor_clamp_percent(max(totals.values(), default=0.0))
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def close(self):
        if self._pdh is not None and self._query is not None:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except (AttributeError, OSError):
                pass
        self.available = False


class _MonitorGpuProbe:
    """Prefer NVIDIA NVML and fall back to Windows GPU engine counters."""

    def __init__(self):
        self._nvml_ready = False
        self._handle = None
        self._engine = _MonitorWindowsGpuEngineReader()
        if _load_monitor_pynvml() is None:
            return
        try:
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_ready = True
            else:
                pynvml.nvmlShutdown()
        except Exception as exc:
            LOG.debug("NVML telemetry unavailable: %s", exc)
            self._nvml_ready = False
            self._handle = None

    @staticmethod
    def _safe(call, default=None):
        try:
            return call()
        except Exception:
            return default

    def sample(self):
        result = {
            "available": False,
            "source": "unavailable",
            "name": "GPU telemetry unavailable",
            "usage": None,
            "vram_used": None,
            "vram_total": None,
            "vram_percent": None,
            "temperature": None,
            "clock_mhz": None,
            "power_w": None,
            "fan_percent": None,
        }
        if self._nvml_ready and self._handle is not None:
            handle = self._handle
            raw_name = self._safe(lambda: pynvml.nvmlDeviceGetName(handle), "NVIDIA GPU")
            if isinstance(raw_name, bytes):
                raw_name = raw_name.decode("utf-8", errors="replace")
            memory = self._safe(lambda: pynvml.nvmlDeviceGetMemoryInfo(handle))
            utilization = self._safe(lambda: pynvml.nvmlDeviceGetUtilizationRates(handle))
            used = int(memory.used) if memory is not None else None
            total = int(memory.total) if memory is not None else None
            result.update({
                "available": True,
                "source": "NVML",
                "name": str(raw_name),
                "usage": monitor_clamp_percent(utilization.gpu if utilization is not None else None),
                "vram_used": used,
                "vram_total": total,
                "vram_percent": monitor_clamp_percent((used / total * 100.0) if used is not None and total else None),
                "temperature": self._safe(
                    lambda: float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
                ),
                "clock_mhz": self._safe(
                    lambda: float(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS))
                ),
                "power_w": self._safe(lambda: float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0),
                "fan_percent": monitor_clamp_percent(self._safe(lambda: pynvml.nvmlDeviceGetFanSpeed(handle))),
            })
            return result
        usage = self._engine.sample()
        if usage is not None:
            result.update({
                "available": True,
                "source": "Windows PDH",
                "name": "Windows graphics adapter",
                "usage": usage,
            })
        return result

    def close(self):
        self._engine.close()
        if self._nvml_ready and pynvml is not None:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
        self._nvml_ready = False


class HardwareMonitorService:
    """Collect bounded hardware and current-user process telemetry in one worker."""

    def __init__(self, interval=0.75):
        if psutil is None:
            raise RuntimeError("psutil is required for Hardware Monitor")
        self.interval = max(0.35, min(5.0, float(interval)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._latency = _MonitorLatencyProbe()
        self._gpu = _MonitorGpuProbe()
        self._logical_cpus = max(1, psutil.cpu_count(logical=True) or 1)
        self._physical_cpus = psutil.cpu_count(logical=False) or self._logical_cpus
        self._username = self._current_username()
        self._latest = self._empty_snapshot()
        self._previous_disk = self._safe_call(psutil.disk_io_counters)
        self._previous_net = self._safe_call(psutil.net_io_counters)
        self._previous_counter_time = time.monotonic()
        self._slow_cache = {}
        self._last_slow_sample = 0.0
        self._process_cache = ([], 0, 0)
        self._last_process_sample = 0.0
        self._process_sample_interval = 2.0

    @staticmethod
    def _safe_call(callback, default=None):
        try:
            return callback()
        except (psutil.Error, OSError, ValueError, AttributeError):
            return default

    @staticmethod
    def _current_username():
        try:
            return psutil.Process().username()
        except (psutil.Error, OSError):
            return ""

    def _empty_snapshot(self):
        return {
            "timestamp": 0.0,
            "status": "starting",
            "error": "",
            "static": {
                "hostname": socket.gethostname() or platform.node() or "Unknown device",
                "os": f"{platform.system()} {platform.release()}".strip(),
                "architecture": platform.machine() or "Unknown",
                "cpu_model": _monitor_cpu_model_name(),
                "physical_cores": self._physical_cpus,
                "logical_cores": self._logical_cpus,
                "username": self._username or "Current user",
            },
            "cpu": {}, "gpu": {}, "memory": {}, "storage": {}, "network": {},
            "system": {}, "processes": [], "user_process_total": 0,
            "processes_truncated": False,
        }

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        psutil.cpu_percent(interval=None, percpu=True)
        for process in psutil.process_iter():
            try:
                process.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                continue
        self._latency.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hardware-monitor")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._latency.stop()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._gpu.close()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._latest)

    def is_running(self):
        return bool(self._thread is not None and self._thread.is_alive() and not self._stop.is_set())

    def _root_mount(self):
        if MONITOR_IS_WINDOWS:
            return (os.environ.get("SystemDrive") or "C:") + "\\"
        return "/"

    def _sample_temperatures(self):
        readings = []
        if not hasattr(psutil, "sensors_temperatures"):
            return readings
        temperatures = self._safe_call(psutil.sensors_temperatures, {}) or {}
        for group, entries in list(temperatures.items())[:8]:
            for entry in entries[:8]:
                try:
                    current = float(entry.current)
                except (TypeError, ValueError):
                    continue
                if -40.0 < current < 160.0:
                    readings.append({
                        "group": str(group),
                        "sensor": entry.label or group,
                        "temperature": current,
                    })
        return readings[:16]

    def _sample_interfaces(self):
        active = []
        ipv4_count = 0
        stats = self._safe_call(psutil.net_if_stats, {}) or {}
        addresses = self._safe_call(psutil.net_if_addrs, {}) or {}
        for name, stat in list(stats.items())[:64]:
            if not stat.isup:
                continue
            ipv4 = [
                address.address for address in addresses.get(name, [])
                if address.family == socket.AF_INET and not address.address.startswith("127.")
            ]
            ipv4_count += len(ipv4)
            active.append({
                "name": name,
                "speed_mbps": max(0, int(stat.speed or 0)),
                "ipv4": ipv4[:4],
            })
        return active[:32], ipv4_count

    def _sample_processes(self, memory_total):
        records = []
        user_total = 0
        total_threads = 0
        memory_total = max(1, int(memory_total or 1))
        attributes = [
            "pid", "name", "username", "memory_info", "status",
            "num_threads", "exe", "create_time", "cpu_percent",
        ]
        for process in psutil.process_iter(attributes):
            try:
                info = process.info
                username = str(info.get("username") or "")
                if self._username and username.casefold() != self._username.casefold():
                    continue
                user_total += 1
                threads = max(0, int(info.get("num_threads") or 0))
                total_threads += threads
                if len(records) >= MONITOR_PROCESS_SAFETY_LIMIT:
                    continue
                normalized_cpu = monitor_normalize_process_cpu(
                    info.get("cpu_percent"), self._logical_cpus
                ) or 0.0
                memory_info = info.get("memory_info")
                memory_bytes = max(0, int(memory_info.rss)) if memory_info is not None else 0
                memory_percent = monitor_clamp_percent(memory_bytes / memory_total * 100.0) or 0.0
                records.append({
                    "pid": max(0, int(info.get("pid") or 0)),
                    "name": str(info.get("name") or "Unknown process")[:160],
                    "cpu_percent": normalized_cpu,
                    "memory_percent": memory_percent,
                    "memory_bytes": memory_bytes,
                    "threads": threads,
                    "status": str(info.get("status") or "unknown")[:40],
                    "username": username[:160],
                    "executable": str(info.get("exe") or "")[:4096],
                    "create_time": max(0.0, float(info.get("create_time") or 0.0)),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError, TypeError):
                continue
        records.sort(key=lambda item: (item["cpu_percent"], item["memory_bytes"]), reverse=True)
        return records, user_total, total_threads

    def _sample(self):
        timestamp = time.time()
        now = time.monotonic()
        elapsed = max(0.001, now - self._previous_counter_time)
        per_cpu = [monitor_clamp_percent(value) or 0.0 for value in psutil.cpu_percent(interval=None, percpu=True)]
        overall = monitor_clamp_percent(sum(per_cpu) / len(per_cpu) if per_cpu else 0.0) or 0.0
        frequency = self._safe_call(psutil.cpu_freq)
        load_average = self._safe_call(os.getloadavg) if hasattr(os, "getloadavg") else None

        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_usage = psutil.disk_usage(self._root_mount())
        disk_now = self._safe_call(psutil.disk_io_counters)
        network_now = self._safe_call(psutil.net_io_counters)

        def counter_rate(current, previous, field):
            if current is None or previous is None:
                return None
            return max(0.0, (float(getattr(current, field)) - float(getattr(previous, field))) / elapsed)

        disk_read = counter_rate(disk_now, self._previous_disk, "read_bytes")
        disk_write = counter_rate(disk_now, self._previous_disk, "write_bytes")
        network_down = counter_rate(network_now, self._previous_net, "bytes_recv")
        network_up = counter_rate(network_now, self._previous_net, "bytes_sent")
        self._previous_disk = disk_now
        self._previous_net = network_now
        self._previous_counter_time = now

        if now - self._last_slow_sample >= 8.0 or not self._slow_cache:
            interfaces, ipv4_count = self._sample_interfaces()
            battery = self._safe_call(psutil.sensors_battery)
            temperatures = self._sample_temperatures()
            self._slow_cache = {
                "interfaces": interfaces,
                "ipv4_count": ipv4_count,
                "battery": None if battery is None else {
                    "percent": monitor_clamp_percent(battery.percent),
                    "plugged": bool(battery.power_plugged),
                    "seconds_left": int(battery.secsleft),
                },
                "temperatures": temperatures,
            }
            self._last_slow_sample = now

        if now - self._last_process_sample >= self._process_sample_interval or not self._process_cache[0]:
            self._process_cache = self._sample_processes(memory.total)
            self._last_process_sample = now
        processes, user_process_total, total_threads = self._process_cache
        boot_time = self._safe_call(psutil.boot_time, timestamp)
        gpu = self._gpu.sample()
        cpu_temperature = None
        temperatures = self._slow_cache.get("temperatures", [])
        cpu_sensor_markers = ("cpu", "coretemp", "k10temp", "zen", "tctl", "package", "acpi")
        cpu_temperatures = [
            entry["temperature"] for entry in temperatures
            if any(
                marker in f"{entry.get('group', '')} {entry.get('sensor', '')}".casefold()
                for marker in cpu_sensor_markers
            )
        ]
        if cpu_temperatures:
            cpu_temperature = max(cpu_temperatures)

        return {
            "timestamp": timestamp,
            "status": "online",
            "error": "",
            "static": self._latest["static"],
            "cpu": {
                "percent": overall,
                "per_cpu": per_cpu[:128],
                "current_mhz": float(frequency.current) if frequency is not None else None,
                "max_mhz": float(frequency.max) if frequency is not None else None,
                "temperature": cpu_temperature,
                "load_average": list(load_average) if load_average is not None else [],
            },
            "gpu": gpu,
            "memory": {
                "percent": monitor_clamp_percent(memory.percent) or 0.0,
                "used": int(memory.used),
                "available": int(memory.available),
                "total": int(memory.total),
                "swap_percent": monitor_clamp_percent(swap.percent) or 0.0,
                "swap_used": int(swap.used),
                "swap_total": int(swap.total),
            },
            "storage": {
                "percent": monitor_clamp_percent(disk_usage.percent) or 0.0,
                "used": int(disk_usage.used),
                "free": int(disk_usage.free),
                "total": int(disk_usage.total),
                "read_rate": disk_read,
                "write_rate": disk_write,
                "mount": self._root_mount(),
            },
            "network": {
                "download_rate": network_down,
                "upload_rate": network_up,
                "latency_ms": self._latency.value(),
                "interfaces": self._slow_cache.get("interfaces", []),
                "ipv4_count": self._slow_cache.get("ipv4_count", 0),
            },
            "system": {
                "uptime_seconds": max(0, int(timestamp - float(boot_time or timestamp))),
                "process_count": len(psutil.pids()),
                "user_process_count": user_process_total,
                "user_thread_count": total_threads,
                "battery": self._slow_cache.get("battery"),
                "temperatures": self._slow_cache.get("temperatures", []),
            },
            "processes": processes,
            "user_process_total": user_process_total,
            "processes_truncated": user_process_total > len(processes),
        }

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = self._sample()
            except Exception as exc:
                LOG.warning("Hardware telemetry cycle failed: %s", exc)
                with self._lock:
                    snapshot = copy.deepcopy(self._latest)
                snapshot.update({"timestamp": time.time(), "status": "degraded", "error": str(exc)[:512]})
            with self._lock:
                self._latest = snapshot
            delay = max(0.0, self.interval - (time.monotonic() - started))
            self._stop.wait(delay)


root = None


def report_startup_error(message):
    """Show startup failures even when launched by double-click with no console."""
    text = str(message)
    LOG.critical("Startup error: %s", text)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "XVVIIX Launcher — Startup Error", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(f"XVVIIX Launcher startup error: {text}", file=sys.stderr)


def is_admin():
    """Return the current Windows elevation state without triggering UAC."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False

# ==========================================
# CONSTANTS AND PATHS
# ==========================================
def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

if getattr(sys, "frozen", False):
    INSTALL_DIR = os.path.dirname(sys.executable)
else:
    INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))


def choose_data_dir():
    """Use the project directory when writable, otherwise use per-user app data."""
    preferred = INSTALL_DIR
    try:
        os.makedirs(preferred, exist_ok=True)
        probe = os.path.join(preferred, ".xvviix-write-test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return preferred
    except OSError:
        fallback_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        fallback = os.path.join(fallback_root, "XVVIIXLauncher")
        os.makedirs(fallback, exist_ok=True)
        return fallback


BASE_DIR = choose_data_dir()
GAMES_FILE = os.path.join(BASE_DIR, "games.json")
APPS_FILE = os.path.join(BASE_DIR, "apps.json")
FOUNDED_FILE = os.path.join(BASE_DIR, "founded.json")
REPORTS_FILE = os.path.join(BASE_DIR, "reports.json")
ACTIVITY_FILE = os.path.join(BASE_DIR, "activity.json")
ICONS_DIR = os.path.join(BASE_DIR, "icons_cache")
ASSETS_DIR = resource_path("assets")
os.makedirs(ICONS_DIR, exist_ok=True)
STARTUP_FILE_LOG_READY = False
try:
    file_handler = logging.FileHandler(
        os.path.join(BASE_DIR, "xvviix_launcher.log"), encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    LOG.addHandler(file_handler)
    STARTUP_FILE_LOG_READY = True
except OSError:
    pass

STARTUP_DIAGNOSTIC_LIMIT = 96
STARTUP_CHECKPOINT_STATUSES = frozenset({
    "STARTED", "READY", "DEGRADED", "SKIPPED", "ABORTED", "FAILED", "STOPPED",
})
startup_run_id = uuid.uuid4().hex[:12]
startup_diagnostics = []
_startup_checkpoint_sequence = 0
_startup_last_checkpoint_at = _BOOTSTRAP_STARTED_AT
_startup_checkpoint_lock = threading.Lock()


def startup_checkpoint(phase, status="READY", detail=""):
    """Emit one bounded, machine-parseable startup diagnostic to console and log."""
    global _startup_checkpoint_sequence, _startup_last_checkpoint_at
    phase_name = "_".join(str(phase or "UNKNOWN").upper().split())[:64]
    status_name = str(status or "READY").upper()
    if status_name not in STARTUP_CHECKPOINT_STATUSES:
        status_name = "DEGRADED"
    clean_detail = " ".join(str(detail or "").split())[:512]
    with _startup_checkpoint_lock:
        now = time.perf_counter()
        _startup_checkpoint_sequence += 1
        record = {
            "event": "startup_checkpoint",
            "run": startup_run_id,
            "sequence": _startup_checkpoint_sequence,
            "phase": phase_name,
            "status": status_name,
            "elapsed_ms": max(0, int((now - _BOOTSTRAP_STARTED_AT) * 1000)),
            "step_ms": max(0, int((now - _startup_last_checkpoint_at) * 1000)),
            "detail": clean_detail,
        }
        _startup_last_checkpoint_at = now
        startup_diagnostics.append(record)
        if len(startup_diagnostics) > STARTUP_DIAGNOSTIC_LIMIT:
            del startup_diagnostics[:-STARTUP_DIAGNOSTIC_LIMIT]
    level = (
        logging.ERROR if status_name in {"FAILED", "ABORTED"}
        else logging.WARNING if status_name == "DEGRADED"
        else logging.INFO
    )
    try:
        LOG.log(level, "STARTUP_CHECKPOINT %s", json.dumps(record, ensure_ascii=True, separators=(",", ":")))
    except Exception as exc:
        print(f"STARTUP_CHECKPOINT {record} logging_error={exc}", file=sys.stderr, flush=True)
    return dict(record)


def startup_diagnostics_snapshot():
    """Return an isolated copy for diagnostics and startup-flow tests."""
    with _startup_checkpoint_lock:
        return [dict(record) for record in startup_diagnostics]


_import_capabilities = {
    "tk": True,
    "dnd": HAS_DND,
    "pillow": HAS_PIL,
    "cryptography": HAS_CRYPTOGRAPHY,
    "psutil": HAS_PSUTIL,
    "hardware_monitor": HAS_HARDWARE_MONITOR,
    "pygame": HAS_PYGAME,
    "winsound": HAS_WINSOUND,
    "win32com": HAS_WIN32COM,
}
_missing_import_capabilities = [name for name, available in _import_capabilities.items() if not available]
startup_checkpoint(
    "BOOTSTRAP_IMPORTS",
    "DEGRADED" if _missing_import_capabilities else "READY",
    "; ".join(f"{name}={'ready' if available else 'unavailable'}" for name, available in _import_capabilities.items()),
)

# Seed user-data files when a packaged install directory is read-only.
if BASE_DIR != INSTALL_DIR:
    for filename in (
        "games.json", "apps.json", "founded.json", "reports.json", "activity.json",
    ):
        source = os.path.join(INSTALL_DIR, filename)
        destination = os.path.join(BASE_DIR, filename)
        if os.path.isfile(source) and not os.path.exists(destination):
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                LOG.warning("Could not seed %s: %s", filename, exc)

startup_checkpoint(
    "DATA_DIRECTORY",
    "READY" if STARTUP_FILE_LOG_READY else "DEGRADED",
    f"writable_root={BASE_DIR}; install_local={BASE_DIR == INSTALL_DIR}; icon_cache=ready; diagnostic_log={'ready' if STARTUP_FILE_LOG_READY else 'console_only'}",
)

SOUNDS = {
    "select_option": resource_path("assets/tunetank.com_menu-interface-selection.wav"),
    "hover": resource_path("assets/tunetank.com_menu-option-hover.wav"),
    "click": resource_path("assets/tunetank.com_option-hover-click.wav"),
    "cursor": resource_path("assets/tunetank.com_interface-cursor-click.wav")
}

# Premium cinematic ambience, mastered below interface cues for long sessions.
MUSIC_ID = "galactic-odyssey-v1"
MUSIC_FILE = resource_path("assets/xvviix_music_galactic_odyssey.ogg")
MUSIC_TITLE = "Galactic Odyssey"
MUSIC_ARTIST = "AlkaKrab"
MUSIC_VOLUME = 0.30
SETTINGS_FILE = os.path.join(BASE_DIR, "launcher_settings.json")
VAULT_FILE = os.path.join(BASE_DIR, "xvviix_vault.json")
VAULT_FORMAT = "xvviix-vault-v1"
ENCRYPTED_DATA_FORMAT = "xvviix-encrypted-json-v1"
VAULT_AAD = b"XVVIIX_ENCRYPTED_DATA_V1"
VAULT_VERIFIER_MESSAGE = b"XVVIIX_VAULT_PASSWORD_VERIFIER_V1"
VAULT_SCRYPT_N = 32768
VAULT_SCRYPT_R = 8
VAULT_SCRYPT_P = 1

# Premium midnight-neon palette
BG = "#060914"
BG2 = "#0a1020"
TOP_BG = "#080d1a"
CARD = "#0f172a"
CARD2 = "#17213a"
TEXT = "#f8fafc"
SUBTEXT = "#8da0bd"
MUTED = "#586a86"
ACCENT = "#8b5cf6"
ACCENT2 = "#a78bfa"
NEON = "#67e8f9"
CYAN = "#22d3ee"
TAB_ACT = "#7c3aed"
TAB_IN = "#111a2e"
GREEN = "#14b8a6"
GREEN_HOVER = "#2dd4bf"
RED = "#f43f5e"
ORANGE = "#f59e0b"
ORANGE_HOVER = "#fbbf24"
BORDER = "#243251"
BORDER_SOFT = "#18233b"

# Windows DWM constants
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

# Global variables
current_scale = 1.0
active_tab = "games"
current_sort = "pinned"
tracked_processes = {}
_anims = {}
_anim_tokens = {}
_animation_serial = 0
_card_leave_jobs = {}
icon_cache = {}
card_art_cache = {}
CARD_ART_CACHE_LIMIT = 96
resize_job = None
search_job = None
PAGE_SIZE = 36
REPORT_PAGE_SIZE = 12
REPORT_LIMIT = 250
ACTIVITY_LIMIT = 80
page_by_tab = {"games": 0, "apps": 0, "founded": 0, "reports": 0, "monitor": 0}
drop_zone = None
drop_reset_job = None
DND_ACTIVE = False
icon_refresh_job = None
icon_update_pending = set()
header_canvas = None
header_image_ref = None
header_stats_id = None
activity_rail = None
activity_primary_lbl = None
activity_secondary_lbl = None
music_btn = None
background_music = None
hardware_monitor = None
hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
hardware_monitor_error = ""
hardware_monitor_idle_job = None
monitor_overlay_requested = False
monitor_tab_btn = None
monitor_ui = {}
monitor_refresh_job = None
monitor_process_query_var = None
monitor_process_filter = ""
monitor_process_sort = "cpu"
monitor_history = {
    "cpu": deque(maxlen=90),
    "gpu": deque(maxlen=90),
    "memory": deque(maxlen=90),
    "storage": deque(maxlen=90),
}
monitor_history_timestamp = 0.0
monitor_overlay = None
monitor_overlay_ui = {}
monitor_overlay_job = None
launcher_settings = {
    "music_enabled": True,
    "music_volume": MUSIC_VOLUME,
    "music_track": MUSIC_ID,
    "card_art_enabled": True,
}
settings_load_status = "READY"
settings_load_detail = "built-in defaults"
vault_key = None
vault_enabled = os.path.isfile(VAULT_FILE)
data_loaded = False

PALETTE = [
    "#7c3aed", "#2563eb", "#059669", "#dc2626",
    "#d97706", "#db2777", "#0891b2", "#65a30d",
    "#9333ea", "#0d9488", "#ea580c", "#4f46e5"
]

# Shared runtime state
ui_queue = queue.Queue()
data_lock = threading.RLock()
process_lock = threading.RLock()
monitor_stop = threading.Event()
scan_running = False
scan_cancel = False
scan_progress_value = 0.0
all_scanned_items = []
sys_report_running = False
sys_report_cancel = threading.Event()
sys_report_window = None
sys_report_status_lbl = None
sys_report_detail_lbl = None
sys_report_progress = None
selected_report_id = None
report_filter = "all"
load_warnings = []
dirty_playtime_libraries = set()
shortcut_com_state = threading.local()

# ==========================================
# UTILITY FUNCTIONS
# ==========================================
def resolve_shortcut(path):
    if not path.lower().endswith(".lnk"):
        return path
    if not HAS_WIN32COM:
        return path
    try:
        shell = getattr(shortcut_com_state, "shell", None)
        if shell is None:
            shell = win32_client.Dispatch("WScript.Shell")
            shortcut_com_state.shell = shell
        shortcut = shell.CreateShortCut(path)
        return shortcut.Targetpath
    except Exception as exc:
        LOG.debug("Could not resolve shortcut %s: %s", path, exc)
        return path

def random_color():
    return random.choice(PALETTE)

def format_time(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    elif m > 0:
        return f"{m}m"
    else:
        return f"{sec}s"

@lru_cache(maxsize=256)
def hex_to_rgb(h):
    if not h.startswith("#"):
        h = "#ffffff"
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

@lru_cache(maxsize=512)
def lerp_color(c1, c2, t):
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(r1+(r2-r1)*t, g1+(g2-g1)*t, b1+(b2-b1)*t)

def play_sound(sound_key):
    if not HAS_WINSOUND:
        return
    filepath = SOUNDS.get(sound_key)
    if filepath and os.path.exists(filepath):
        try:
            winsound.PlaySound(
                filepath,
                winsound.SND_FILENAME
                | winsound.SND_ASYNC
                | winsound.SND_NOWAIT
                | winsound.SND_NODEFAULT,
            )
        except (OSError, RuntimeError) as exc:
            LOG.debug("Sound playback failed: %s", exc)


def load_launcher_settings():
    """Load small user preferences without allowing a damaged file to block startup."""
    global settings_load_status, settings_load_detail
    settings_load_status = "READY"
    settings_load_detail = "built-in defaults; settings file not present"
    defaults = {
        "music_enabled": True,
        "music_volume": MUSIC_VOLUME,
        "music_track": MUSIC_ID,
        "card_art_enabled": True,
    }
    if not os.path.isfile(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ValueError("settings root must be a JSON object")
        if isinstance(stored.get("music_enabled"), bool):
            defaults["music_enabled"] = stored["music_enabled"]
        if isinstance(stored.get("card_art_enabled"), bool):
            defaults["card_art_enabled"] = stored["card_art_enabled"]
        # A replacement track gets its own calibrated default instead of
        # inheriting a potentially much louder volume from the previous music.
        volume = stored.get("music_volume")
        if (
            stored.get("music_track") == MUSIC_ID
            and isinstance(volume, (int, float))
            and not isinstance(volume, bool)
        ):
            defaults["music_volume"] = max(0.0, min(1.0, float(volume)))
        settings_load_detail = "validated launcher settings file"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        settings_load_status = "DEGRADED"
        settings_load_detail = f"invalid settings file; safe defaults restored: {exc}"
        LOG.warning("Could not load launcher settings: %s", exc)
    return defaults


def save_launcher_settings():
    """Atomically persist music preferences in the writable data directory."""
    temporary = f"{SETTINGS_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(launcher_settings, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SETTINGS_FILE)
    except (OSError, TypeError, ValueError) as exc:
        LOG.warning("Could not save launcher settings: %s", exc)
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


class BackgroundMusic:
    """Loop one ambience track independently from interface sound effects."""

    def __init__(self, filepath, enabled=True, volume=MUSIC_VOLUME):
        self.filepath = filepath
        self._enabled = bool(enabled)
        self.volume = max(0.0, min(1.0, float(volume)))
        self._failed = False
        self._playing = False

    @property
    def available(self):
        audio_backend_ready = HAS_PYGAME if _pygame_import_attempted else True
        return bool(audio_backend_ready and os.path.isfile(self.filepath) and not self._failed)

    @property
    def enabled(self):
        return self._enabled

    def _ensure_mixer(self):
        if load_optional_pygame() is None:
            raise RuntimeError("pygame is unavailable")
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

    def start(self, startup_probe=False):
        if not self._enabled:
            if startup_probe:
                startup_checkpoint("AUDIO_PLAYBACK", "SKIPPED", "disabled by launcher setting")
            return True
        if not self.available:
            if startup_probe:
                startup_checkpoint("AUDIO_PLAYBACK", "DEGRADED", "mixer integration or music asset unavailable")
            return False
        if self._playing:
            if startup_probe:
                startup_checkpoint("AUDIO_PLAYBACK", "READY", f"track={MUSIC_TITLE}; already playing")
            return True
        try:
            self._ensure_mixer()
            pygame.mixer.music.load(self.filepath)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play(loops=-1, fade_ms=700)
            self._playing = True
            LOG.info("Background music started: %s", MUSIC_TITLE)
            if startup_probe:
                startup_checkpoint("AUDIO_PLAYBACK", "READY", f"track={MUSIC_TITLE}; loop=active")
            return True
        except Exception as exc:
            self._failed = True
            self._playing = False
            LOG.warning("Background music could not start: %s", exc)
            if startup_probe:
                startup_checkpoint("AUDIO_PLAYBACK", "DEGRADED", f"mixer initialization failed: {exc}")
            post_ui(update_music_button)
            return False

    def _stop_playback(self, fade_ms=250):
        if not HAS_PYGAME or not pygame.mixer.get_init():
            self._playing = False
            return
        try:
            if fade_ms:
                pygame.mixer.music.fadeout(fade_ms)
            else:
                pygame.mixer.music.stop()
        except Exception as exc:
            LOG.debug("Background music stop failed: %s", exc)
        self._playing = False

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if self._enabled:
            self.start()
        else:
            self._stop_playback()

    def stop(self, timeout=1.0):
        del timeout
        self._stop_playback(fade_ms=0)
        if HAS_PYGAME and pygame.mixer.get_init():
            try:
                pygame.mixer.music.unload()
                pygame.mixer.quit()
            except Exception as exc:
                LOG.debug("Background mixer cleanup failed: %s", exc)


def update_music_button():
    if not widget_exists(music_btn) or background_music is None:
        return
    if not background_music.available:
        music_btn._anim_idle_bg = BG2
        music_btn._anim_hover_bg = CARD2
        music_btn._anim_idle_fg = MUTED
        music_btn._anim_hover_fg = MUTED
        music_btn.configure(
            text="♫  AUDIO UNAVAILABLE", state="disabled", bg=BG2, fg=MUTED,
            disabledforeground=MUTED,
        )
        return

    enabled = background_music.enabled
    idle_bg = "#10243a" if enabled else BG2
    hover_bg = "#164e63" if enabled else CARD2
    foreground = NEON if enabled else MUTED
    music_btn._anim_idle_bg = idle_bg
    music_btn._anim_hover_bg = hover_bg
    music_btn._anim_idle_fg = foreground
    music_btn._anim_hover_fg = TEXT
    music_btn.configure(
        text="♫  GALACTIC  ON" if enabled else "♫  GALACTIC  OFF",
        state="normal", bg=idle_bg, fg=foreground,
        activebackground=hover_bg, activeforeground=TEXT,
    )


def toggle_background_music():
    if background_music is None or not background_music.available:
        return
    enabled = not background_music.enabled
    background_music.set_enabled(enabled)
    launcher_settings["music_enabled"] = enabled
    launcher_settings["music_volume"] = background_music.volume
    launcher_settings["music_track"] = MUSIC_ID
    save_launcher_settings()
    update_music_button()


def show_music_credit(_event=None):
    messagebox.showinfo(
        "XVVIIX music credit",
        f"{MUSIC_TITLE}\n{MUSIC_ARTIST}\n\n"
        "Music by AlkaKrab\n"
        "From Free Sci-Fi Music Pack Vol. 2",
    )

# ==========================================
# FILE OPERATIONS AND PASSWORD VAULT
# ==========================================
class VaultError(Exception):
    pass


class VaultPasswordError(VaultError):
    pass


def protected_data_files():
    """Return user-data JSON files whose payload must be encrypted at rest."""
    return (GAMES_FILE, APPS_FILE, FOUNDED_FILE, REPORTS_FILE, ACTIVITY_FILE)


def is_protected_data_file(filepath):
    target = os.path.abspath(filepath)
    return any(target == os.path.abspath(path) for path in protected_data_files())


def _decode_base64(value, label, minimum=1, maximum=1024 * 1024 * 64):
    try:
        decoded = base64.b64decode(str(value).encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise VaultError(f"Invalid {label} encoding") from exc
    if not minimum <= len(decoded) <= maximum:
        raise VaultError(f"Invalid {label} length")
    return decoded


def derive_vault_key(password, metadata):
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise VaultPasswordError("Invalid password")
    if not isinstance(metadata, dict) or metadata.get("format") != VAULT_FORMAT:
        raise VaultError("Unsupported or damaged vault metadata")
    kdf = metadata.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise VaultError("Unsupported password derivation format")
    try:
        n = int(kdf.get("n"))
        r = int(kdf.get("r"))
        p = int(kdf.get("p"))
    except (TypeError, ValueError) as exc:
        raise VaultError("Damaged password derivation settings") from exc
    if n < 16384 or n > 131072 or n & (n - 1) or not 1 <= r <= 16 or not 1 <= p <= 4:
        raise VaultError("Unsafe password derivation settings were rejected")
    salt = _decode_base64(kdf.get("salt", ""), "vault salt", 16, 64)
    try:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        raise VaultError(f"Password derivation failed: {exc}") from exc


def create_vault_metadata(password):
    salt = os.urandom(16)
    metadata = {
        "format": VAULT_FORMAT,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "n": VAULT_SCRYPT_N,
            "r": VAULT_SCRYPT_R,
            "p": VAULT_SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "created": record_timestamp() if "record_timestamp" in globals() else datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    key = derive_vault_key(password, metadata)
    metadata["verifier"] = base64.b64encode(
        hmac.new(key, VAULT_VERIFIER_MESSAGE, hashlib.sha256).digest()
    ).decode("ascii")
    return metadata, key


def verify_vault_password(password, metadata):
    key = derive_vault_key(password, metadata)
    expected = _decode_base64(metadata.get("verifier", ""), "password verifier", 32, 32)
    actual = hmac.new(key, VAULT_VERIFIER_MESSAGE, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        raise VaultPasswordError("Incorrect password")
    return key


def encrypt_data_bytes(plaintext, key):
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    if not isinstance(plaintext, bytes) or not isinstance(key, bytes) or len(key) != 32:
        raise VaultError("Invalid encryption input")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, VAULT_AAD)
    envelope = {
        "format": ENCRYPTED_DATA_FORMAT,
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(envelope, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def decrypt_data_envelope(envelope, key):
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    if not isinstance(envelope, dict) or envelope.get("format") != ENCRYPTED_DATA_FORMAT:
        raise VaultError("Unsupported encrypted data format")
    nonce = _decode_base64(envelope.get("nonce", ""), "encryption nonce", 12, 12)
    ciphertext = _decode_base64(
        envelope.get("ciphertext", ""), "encrypted payload", 16, 256 * 1024 * 1024,
    )
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, VAULT_AAD)
    except InvalidTag as exc:
        raise VaultError("Encrypted data authentication failed") from exc
    except (ValueError, TypeError) as exc:
        raise VaultError(f"Encrypted data could not be opened: {exc}") from exc


def _parse_json_bytes(payload):
    return json.loads(payload.decode("utf-8"))


def _is_encrypted_payload(payload):
    try:
        parsed = _parse_json_bytes(payload)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and parsed.get("format") == ENCRYPTED_DATA_FORMAT


def read_data_json(filepath):
    with open(filepath, "rb") as handle:
        payload = handle.read()
    parsed = _parse_json_bytes(payload)
    if isinstance(parsed, dict) and parsed.get("format") == ENCRYPTED_DATA_FORMAT:
        if vault_key is None:
            raise VaultError("The data vault is locked")
        plaintext = decrypt_data_envelope(parsed, vault_key)
        return _parse_json_bytes(plaintext)
    return parsed


def _atomic_write_bytes(filepath, payload):
    directory = os.path.dirname(filepath) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(filepath)}.", suffix=".tmp", dir=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filepath)
        temporary = ""
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _write_vault_metadata(metadata):
    payload = (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    _atomic_write_bytes(VAULT_FILE, payload)
    try:
        shutil.copy2(VAULT_FILE, f"{VAULT_FILE}.bak")
    except OSError as exc:
        LOG.warning("Could not create vault metadata backup: %s", exc)


def load_vault_metadata():
    failures = []
    for candidate in (VAULT_FILE, f"{VAULT_FILE}.bak"):
        if not os.path.isfile(candidate):
            continue
        try:
            if os.path.getsize(candidate) > 64 * 1024:
                raise VaultError("Vault metadata exceeds the safety limit")
            with open(candidate, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            if not isinstance(metadata, dict) or metadata.get("format") != VAULT_FORMAT:
                raise VaultError("Unsupported vault metadata")
            if candidate != VAULT_FILE:
                _atomic_write_bytes(
                    VAULT_FILE,
                    (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
                )
                LOG.warning("Restored vault metadata from its backup")
            return metadata
        except (OSError, UnicodeError, json.JSONDecodeError, VaultError) as exc:
            failures.append(f"{os.path.basename(candidate)}: {exc}")
    raise VaultError("Vault metadata is missing or damaged. " + " | ".join(failures))


def protected_artifact_paths():
    paths = []
    for primary in protected_data_files():
        paths.append(primary)
        paths.extend(glob.glob(f"{primary}.bak"))
        paths.extend(glob.glob(f"{primary}.corrupt-*"))
    return list(dict.fromkeys(paths))


def encrypted_data_exists_without_vault():
    marker = ENCRYPTED_DATA_FORMAT.encode("ascii")
    for filepath in protected_artifact_paths():
        if not os.path.isfile(filepath):
            continue
        try:
            with open(filepath, "rb") as handle:
                if marker in handle.read(4096):
                    return True
        except OSError:
            continue
    return False


def ensure_protected_files_encrypted():
    if vault_key is None:
        raise VaultError("The data vault is locked")
    for primary in protected_data_files():
        if not os.path.exists(primary):
            _atomic_write_bytes(primary, encrypt_data_bytes(b"[]\n", vault_key))
    for filepath in protected_artifact_paths():
        if not os.path.isfile(filepath):
            continue
        with open(filepath, "rb") as handle:
            payload = handle.read()
        if _is_encrypted_payload(payload):
            envelope = _parse_json_bytes(payload)
            decrypt_data_envelope(envelope, vault_key)
            continue
        _atomic_write_bytes(filepath, encrypt_data_bytes(payload, vault_key))


def create_data_vault(password):
    """Stage every ciphertext before committing vault metadata and data files."""
    global vault_key, vault_enabled
    if not HAS_CRYPTOGRAPHY:
        raise VaultError("Install the cryptography package before creating the vault")
    if os.path.exists(VAULT_FILE) or os.path.exists(f"{VAULT_FILE}.bak"):
        raise VaultError("A data vault already exists; restart XVVIIX and unlock it")
    metadata, key = create_vault_metadata(password)
    staged = []
    try:
        existing = protected_artifact_paths()
        targets = list(dict.fromkeys(list(protected_data_files()) + existing))
        for filepath in targets:
            if os.path.isfile(filepath):
                with open(filepath, "rb") as handle:
                    plaintext = handle.read()
                if _is_encrypted_payload(plaintext):
                    raise VaultError(
                        "Encrypted data already exists. Restore the matching vault metadata before setup."
                    )
            else:
                plaintext = b"[]\n"
            payload = encrypt_data_bytes(plaintext, key)
            directory = os.path.dirname(filepath) or "."
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{os.path.basename(filepath)}.", suffix=".vault-stage", dir=directory,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((temporary, filepath))

        _write_vault_metadata(metadata)
        vault_key = key
        vault_enabled = True
        for temporary, filepath in staged:
            os.replace(temporary, filepath)
        staged.clear()
        ensure_protected_files_encrypted()
        return True
    finally:
        for temporary, _filepath in staged:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass


def unlock_data_vault(password):
    global vault_key, vault_enabled
    metadata = load_vault_metadata()
    key = verify_vault_password(password, metadata)
    vault_key = key
    vault_enabled = True
    try:
        ensure_protected_files_encrypted()
    except Exception:
        vault_key = None
        raise
    return True


def clear_vault_key():
    global vault_key
    vault_key = None


IDENTITY_TEXT_FIELDS = (
    "publisher", "product", "description", "original_filename",
    "internal_name", "source", "start_menu_group", "kind",
)


def normalize_identity(raw):
    """Keep only bounded scanner identity data that is safe to persist."""
    if not isinstance(raw, dict):
        return {}
    identity = {}
    for field in IDENTITY_TEXT_FIELDS:
        value = str(raw.get(field, "") or "").strip()
        if value:
            identity[field] = value[:512]
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence:
        identity["confidence"] = round(max(0.0, min(1.0, confidence)), 3)
    return identity


def normalize_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("library entries must be JSON objects")
    name = str(raw.get("name", "")).strip()
    path = str(raw.get("path", "")).strip()
    if not name or not path:
        raise ValueError("library entries require a name and path")
    try:
        playtime = max(0, int(raw.get("playtime", 0)))
    except (TypeError, ValueError):
        playtime = 0
    color = str(raw.get("color", ""))
    try:
        hex_to_rgb(color)
    except (TypeError, ValueError):
        color = random_color()
    item = {
        "name": name,
        "path": path,
        "trainer": str(raw.get("trainer", "") or ""),
        "icon": str(raw.get("icon", "") or ""),
        "playtime": playtime,
        "pinned": bool(raw.get("pinned", False)),
        "color": color,
    }
    artwork = str(raw.get("artwork", "") or "").strip()
    if artwork:
        item["artwork"] = artwork[:4096]
    identity = normalize_identity(raw.get("identity"))
    if identity:
        item["identity"] = identity
    return item


def _bounded_record_text(value, limit=1024):
    return str(value or "").strip()[:limit]


def normalize_report_sections(raw_sections):
    if not isinstance(raw_sections, list):
        return []
    sections = []
    for raw_section in raw_sections[:16]:
        if not isinstance(raw_section, dict):
            continue
        items = []
        raw_items = raw_section.get("items", [])
        if isinstance(raw_items, list):
            for raw_item in raw_items[:24]:
                if not isinstance(raw_item, dict):
                    continue
                label = _bounded_record_text(raw_item.get("label"), 128)
                value = _bounded_record_text(raw_item.get("value"), 1024)
                if label and value:
                    items.append({
                        "label": label,
                        "value": value,
                        "status": _bounded_record_text(raw_item.get("status"), 24).lower() or "info",
                    })
        title = _bounded_record_text(raw_section.get("title"), 128)
        if title and items:
            sections.append({
                "title": title,
                "status": _bounded_record_text(raw_section.get("status"), 24).lower() or "info",
                "items": items,
            })
    return sections


def normalize_report_findings(raw_findings):
    if not isinstance(raw_findings, list):
        return []
    findings = []
    for raw in raw_findings[:32]:
        if not isinstance(raw, dict):
            continue
        title = _bounded_record_text(raw.get("title"), 256)
        if title:
            findings.append({
                "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "info",
                "title": title,
                "detail": _bounded_record_text(raw.get("detail"), 1024),
                "action": _bounded_record_text(raw.get("action"), 512),
            })
    return findings


def normalize_report_summary(raw_summary):
    if not isinstance(raw_summary, dict):
        return {}
    summary = {}
    for key, value in list(raw_summary.items())[:24]:
        clean_key = _bounded_record_text(key, 64)
        clean_value = _bounded_record_text(value, 512)
        if clean_key and clean_value:
            summary[clean_key] = clean_value
    return summary


def normalize_report(raw):
    """Validate one locally generated report while keeping future report kinds extensible."""
    if not isinstance(raw, dict):
        raise ValueError("report entries must be JSON objects")
    report_id = _bounded_record_text(raw.get("id"), 80) or uuid.uuid4().hex
    kind = _bounded_record_text(raw.get("kind"), 64) or "game_crash"
    timestamp = _bounded_record_text(raw.get("timestamp"), 80)
    try:
        epoch = max(0.0, float(raw.get("epoch", 0.0)))
    except (TypeError, ValueError):
        epoch = 0.0
    try:
        pid = max(0, int(raw.get("pid", 0)))
    except (TypeError, ValueError):
        pid = 0
    try:
        runtime_seconds = max(0, int(raw.get("runtime_seconds", 0)))
    except (TypeError, ValueError):
        runtime_seconds = 0
    exit_code = raw.get("exit_code")
    try:
        exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None
    try:
        health_score = max(0, min(100, int(raw.get("health_score", 0))))
    except (TypeError, ValueError):
        health_score = 0
    try:
        scan_duration_ms = max(0, min(3_600_000, int(raw.get("scan_duration_ms", 0))))
    except (TypeError, ValueError):
        scan_duration_ms = 0
    suggestions = raw.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    return {
        "id": report_id,
        "kind": kind,
        "timestamp": timestamp,
        "epoch": epoch,
        "title": _bounded_record_text(raw.get("title"), 256) or "Game crash report",
        "item_name": _bounded_record_text(raw.get("item_name"), 256),
        "item_path": _bounded_record_text(raw.get("item_path"), 4096),
        "pid": pid,
        "exit_code": exit_code,
        "exit_hex": _bounded_record_text(raw.get("exit_hex"), 32),
        "cause": _bounded_record_text(raw.get("cause"), 512) or "Unknown failure",
        "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "warning",
        "runtime_seconds": runtime_seconds,
        "details": _bounded_record_text(raw.get("details"), 4096),
        "fault_module": _bounded_record_text(raw.get("fault_module"), 512),
        "source": _bounded_record_text(raw.get("source"), 128) or "xvviix_process_monitor",
        "health_score": health_score,
        "scan_duration_ms": scan_duration_ms,
        "summary": normalize_report_summary(raw.get("summary")),
        "sections": normalize_report_sections(raw.get("sections")),
        "findings": normalize_report_findings(raw.get("findings")),
        "suggestions": [
            _bounded_record_text(value, 512) for value in suggestions[:8]
            if _bounded_record_text(value, 512)
        ],
    }


def normalize_activity(raw):
    if not isinstance(raw, dict):
        raise ValueError("activity entries must be JSON objects")
    try:
        epoch = max(0.0, float(raw.get("epoch", 0.0)))
    except (TypeError, ValueError):
        epoch = 0.0
    return {
        "id": _bounded_record_text(raw.get("id"), 80) or uuid.uuid4().hex,
        "kind": _bounded_record_text(raw.get("kind"), 64) or "status",
        "timestamp": _bounded_record_text(raw.get("timestamp"), 80),
        "epoch": epoch,
        "title": _bounded_record_text(raw.get("title"), 256) or "Launcher activity",
        "item_name": _bounded_record_text(raw.get("item_name"), 256),
        "item_path": _bounded_record_text(raw.get("item_path"), 4096),
        "detail": _bounded_record_text(raw.get("detail"), 1024),
        "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "info",
    }


def _load_auxiliary_records(filepath, normalizer, limit):
    if not os.path.exists(filepath):
        return []
    try:
        raw_data = read_data_json(filepath)
        if not isinstance(raw_data, list):
            raise ValueError("data root must be a JSON array")
        result = []
        for index, raw in enumerate(raw_data[:limit]):
            try:
                result.append(normalizer(raw))
            except ValueError as exc:
                LOG.warning("Skipped invalid entry %s in %s: %s", index, filepath, exc)
        result.sort(key=lambda record: record.get("epoch", 0.0), reverse=True)
        return result
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        recovery_path = f"{filepath}.corrupt-{timestamp}"
        try:
            shutil.copy2(filepath, recovery_path)
        except OSError:
            recovery_path = "(backup could not be created)"
        warning = f"Could not load {os.path.basename(filepath)}: {exc}. Recovery: {recovery_path}"
        load_warnings.append(warning)
        LOG.error(warning)
        return []


def _load(filepath):
    if not os.path.exists(filepath):
        return []
    try:
        raw_data = read_data_json(filepath)
        if not isinstance(raw_data, list):
            raise ValueError("library root must be a JSON array")
        result = []
        for index, raw in enumerate(raw_data):
            try:
                result.append(normalize_item(raw))
            except ValueError as exc:
                LOG.warning("Skipped invalid entry %s in %s: %s", index, filepath, exc)
        return result
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        recovery_path = f"{filepath}.corrupt-{timestamp}"
        try:
            shutil.copy2(filepath, recovery_path)
        except OSError:
            recovery_path = "(backup could not be created)"
        warning = f"Could not load {os.path.basename(filepath)}: {exc}. Recovery: {recovery_path}"
        load_warnings.append(warning)
        LOG.error(warning)
        return []


def _save(filepath, data):
    """Atomically save data; protected files and their backups remain encrypted."""
    directory = os.path.dirname(filepath) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    with data_lock:
        try:
            plaintext = (json.dumps(data, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
            if is_protected_data_file(filepath):
                if not vault_enabled or vault_key is None:
                    raise VaultError("Refused to write protected data while the vault is locked")
                payload = encrypt_data_bytes(plaintext, vault_key)
            else:
                payload = plaintext
            descriptor, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(filepath)}.", suffix=".tmp", dir=directory
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.exists(filepath):
                shutil.copy2(filepath, f"{filepath}.bak")
            os.replace(temp_path, filepath)
            temp_path = None
            return True
        except (OSError, TypeError, ValueError, VaultError) as exc:
            LOG.error("Could not save %s: %s", filepath, exc)
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


games = []
apps = []
founded = []
reports = []
recent_activity = []


def record_timestamp(epoch=None):
    moment = datetime.fromtimestamp(epoch).astimezone() if epoch else datetime.now().astimezone()
    return moment.isoformat(timespec="seconds")


def load_all_data_files():
    """Load protected user data only after the vault key is available."""
    global games, apps, founded, reports, recent_activity, data_loaded
    loaded_games = _load(GAMES_FILE)
    loaded_apps = _load(APPS_FILE)
    loaded_founded = _load(FOUNDED_FILE)
    loaded_reports = _load_auxiliary_records(REPORTS_FILE, normalize_report, REPORT_LIMIT)
    loaded_activity = _load_auxiliary_records(ACTIVITY_FILE, normalize_activity, ACTIVITY_LIMIT)
    games = loaded_games
    apps = loaded_apps
    founded = loaded_founded
    reports = loaded_reports
    recent_activity = loaded_activity
    data_loaded = True
    return True


def show_vault_dialog(parent, setup=False):
    """Display a blocking password setup/unlock dialog without storing the password."""
    result = {"unlocked": False}
    dialog = tk.Toplevel(parent)
    dialog.title("XVVIIX Data Vault")
    dialog.configure(bg=BG)
    dialog.resizable(False, False)
    dialog.grab_set()
    width, height = 520, 405 if setup else 335
    x = (dialog.winfo_screenwidth() - width) // 2
    y = (dialog.winfo_screenheight() - height) // 2
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    enable_win11_round_corners(dialog)

    tk.Frame(dialog, bg=ACCENT, height=4).pack(fill="x")
    body = tk.Frame(dialog, bg=BG, padx=32, pady=24)
    body.pack(fill="both", expand=True)
    tk.Label(
        body, text="◆  XVVIIX DATA VAULT", bg=BG, fg=TEXT,
        font=("Segoe UI Black", 18, "bold"),
    ).pack(anchor="w")
    tk.Label(
        body,
        text="CREATE A MASTER PASSWORD" if setup else "ENCRYPTED LIBRARIES DETECTED",
        bg=BG, fg=NEON, font=("Consolas", 9, "bold"),
    ).pack(anchor="w", pady=(5, 18))
    tk.Label(
        body,
        text=(
            "Games, Workspace, Discovered, Reports, and Activity will be encrypted. "
            "The password cannot be recovered."
            if setup else
            "Enter your master password to decrypt the launcher data for this session."
        ),
        bg=BG, fg=SUBTEXT, font=("Segoe UI", 9),
        justify="left", wraplength=450,
    ).pack(anchor="w", pady=(0, 15))

    password_var = tk.StringVar()
    confirm_var = tk.StringVar()
    tk.Label(
        body, text="MASTER PASSWORD", bg=BG, fg=MUTED,
        font=("Segoe UI", 8, "bold"),
    ).pack(anchor="w", pady=(0, 4))
    password_entry = tk.Entry(
        body, textvariable=password_var, show="●", bg=BG2, fg=TEXT,
        insertbackground=NEON, relief="flat", highlightbackground=BORDER,
        highlightcolor=NEON, highlightthickness=1, font=("Segoe UI", 11),
    )
    password_entry.pack(fill="x", ipady=8)

    if setup:
        tk.Label(
            body, text="CONFIRM PASSWORD", bg=BG, fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(12, 4))
        confirm_entry = tk.Entry(
            body, textvariable=confirm_var, show="●", bg=BG2, fg=TEXT,
            insertbackground=NEON, relief="flat", highlightbackground=BORDER,
            highlightcolor=NEON, highlightthickness=1, font=("Segoe UI", 11),
        )
        confirm_entry.pack(fill="x", ipady=8)

    status_label = tk.Label(
        body,
        text="MINIMUM 8 CHARACTERS  ·  USE A UNIQUE PASSPHRASE" if setup else "AES-256-GCM  ·  SCRYPT KEY DERIVATION",
        bg=BG, fg=MUTED, font=("Consolas", 8, "bold"), anchor="w",
    )
    status_label.pack(fill="x", pady=(12, 8))
    actions = tk.Frame(body, bg=BG)
    actions.pack(fill="x", side="bottom")

    def cancel():
        password_var.set("")
        confirm_var.set("")
        try:
            dialog.grab_release()
            dialog.destroy()
        except tk.TclError:
            pass

    def submit():
        password = password_var.get()
        if setup:
            if len(password) < 8:
                status_label.config(text="PASSWORD MUST CONTAIN AT LEAST 8 CHARACTERS", fg=RED)
                return
            if password != confirm_var.get():
                status_label.config(text="PASSWORDS DO NOT MATCH", fg=RED)
                return
        status_label.config(text="DERIVING KEY AND SECURING DATA...", fg=ORANGE)
        submit_button.config(state="disabled")
        dialog.update_idletasks()
        try:
            if setup:
                create_data_vault(password)
            else:
                unlock_data_vault(password)
            load_all_data_files()
            result["unlocked"] = True
            password_var.set("")
            confirm_var.set("")
            dialog.grab_release()
            dialog.destroy()
        except VaultPasswordError:
            clear_vault_key()
            status_label.config(text="INCORRECT PASSWORD", fg=RED)
            password_entry.selection_range(0, tk.END)
            password_entry.focus_set()
            submit_button.config(state="normal")
        except (VaultError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            clear_vault_key()
            metadata_committed = setup and os.path.isfile(VAULT_FILE)
            status_label.config(
                text="RESTART XVVIIX TO RESUME VAULT SETUP" if metadata_committed else "VAULT ERROR — SEE MESSAGE",
                fg=RED,
            )
            submit_button.config(state="disabled" if metadata_committed else "normal")
            messagebox.showerror("XVVIIX Data Vault", str(exc), parent=dialog)
        finally:
            password = ""

    cancel_button = tk.Button(
        actions, text="EXIT", bg=CARD2, fg=SUBTEXT, relief="flat",
        font=("Segoe UI", 9, "bold"), padx=20, pady=9,
        cursor="hand2", bd=0, command=cancel,
    )
    cancel_button.pack(side="left")
    submit_button = tk.Button(
        actions, text="CREATE VAULT" if setup else "UNLOCK XVVIIX",
        bg=ACCENT, fg=TEXT, relief="flat",
        font=("Segoe UI", 9, "bold"), padx=22, pady=9,
        cursor="hand2", bd=0, command=submit,
    )
    submit_button.pack(side="right")
    bind_animated_button(cancel_button, CARD2, RED, SUBTEXT, TEXT)
    bind_animated_button(submit_button, ACCENT, ACCENT2, TEXT, TEXT)
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.bind("<Escape>", lambda _event: cancel())
    dialog.bind("<Return>", lambda _event: submit())
    dialog.deiconify()
    dialog.lift()
    password_entry.focus_set()
    parent.wait_window(dialog)
    return result["unlocked"]


def initialize_data_vault(parent):
    """Require setup or unlock before any protected library data is exposed."""
    global vault_enabled
    if not HAS_CRYPTOGRAPHY:
        messagebox.showerror(
            "XVVIIX Data Vault",
            "Password protection requires the cryptography package.\n\n"
            "Run:  pip install -r requirements.txt",
            parent=parent,
        )
        return False
    metadata_exists = os.path.isfile(VAULT_FILE) or os.path.isfile(f"{VAULT_FILE}.bak")
    if not metadata_exists and encrypted_data_exists_without_vault():
        messagebox.showerror(
            "XVVIIX Data Vault",
            "Encrypted library data exists, but xvviix_vault.json is missing.\n\n"
            "Restore xvviix_vault.json or xvviix_vault.json.bak before continuing.",
            parent=parent,
        )
        return False
    vault_enabled = metadata_exists
    return show_vault_dialog(parent, setup=not metadata_exists)


def add_activity(kind, title, item=None, detail="", severity="info", epoch=None):
    """Persist one bounded activity signal and schedule the header rail update."""
    event_epoch = float(epoch or time.time())
    item = item if isinstance(item, dict) else {}
    event = normalize_activity({
        "id": uuid.uuid4().hex,
        "kind": kind,
        "timestamp": record_timestamp(event_epoch),
        "epoch": event_epoch,
        "title": title,
        "item_name": item.get("name", ""),
        "item_path": item.get("path", ""),
        "detail": detail,
        "severity": severity,
    })
    with data_lock:
        recent_activity.insert(0, event)
        del recent_activity[ACTIVITY_LIMIT:]
    _save(ACTIVITY_FILE, recent_activity)
    if root is not None:
        post_ui(update_activity_rail)
    return event


def add_report(report):
    """Persist a normalized report without allowing unbounded report growth."""
    normalized = normalize_report(report)
    with data_lock:
        previous = list(reports)
        reports.insert(0, normalized)
        del reports[REPORT_LIMIT:]
    if not _save(REPORTS_FILE, reports):
        with data_lock:
            reports[:] = previous
        return None
    if root is not None:
        post_ui(refresh)
    return normalized


def current_list():
    return {"games": games, "apps": apps, "founded": founded}.get(active_tab, games)


def save_current(update_ui=False):
    targets = {
        "games": (GAMES_FILE, games),
        "apps": (APPS_FILE, apps),
        "founded": (FOUNDED_FILE, founded),
    }
    filepath, data = targets.get(active_tab, targets["games"])
    saved = _save(filepath, data)
    if saved:
        dirty_playtime_libraries.discard(active_tab)
    if update_ui and root is not None:
        update_stats()
    return saved


def save_item_library(item):
    if any(candidate is item for candidate in games):
        return _save(GAMES_FILE, games)
    if any(candidate is item for candidate in apps):
        return _save(APPS_FILE, apps)
    if any(candidate is item for candidate in founded):
        return _save(FOUNDED_FILE, founded)
    return False


def post_ui(callback, *args, **kwargs):
    ui_queue.put((callback, args, kwargs))


def process_ui_queue():
    """Process worker results within a small time budget to keep Tk responsive."""
    if root is None:
        return
    deadline = time.perf_counter() + 0.008
    processed = 0
    while processed < 100 and time.perf_counter() < deadline:
        try:
            callback, args, kwargs = ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            callback(*args, **kwargs)
        except (tk.TclError, RuntimeError):
            LOG.debug("Discarded a UI callback during shutdown")
        processed += 1
    delay = 16 if not ui_queue.empty() else 100
    try:
        root.after(delay, process_ui_queue)
    except tk.TclError:
        pass

# ==========================================
# SCANNER FUNCTIONS
# ==========================================
DRIVER_MARKERS = (
    " driver ", " device driver ", " display driver ", " graphics driver ",
    " audio driver ", " chipset driver ", " network driver ",
    " printer driver ", " bluetooth driver ", " wireless driver ",
    " driver package ", " firmware ", " kernel driver ",
)
UNNECESSARY_MARKERS = (
    " uninstall ", " uninstaller ", " setup ", " installer ",
    " updater ", " update service ", " crash reporter ",
    " crashreporter ", " crashpad handler ", " telemetry service ",
    " maintenance service ", " redistributable ", " redist ",
    " runtime installer ", " repair tool ",
)
UNNECESSARY_BASENAMES = {
    "uninstall.exe", "uninstaller.exe", "unins000.exe", "unins001.exe",
    "setup.exe", "installer.exe", "update.exe", "updater.exe",
    "crashreporter.exe", "crashpad_handler.exe", "helper.exe",
    "service.exe", "maintenanceservice.exe", "vc_redist.x86.exe",
    "vc_redist.x64.exe", "dxsetup.exe", "msiexec.exe",
}
SYSTEM_PATH_MARKERS = (
    "\\windows\\system32\\", "\\windows\\syswow64\\",
    "\\windows\\winsxs\\", "\\windows\\servicing\\",
    "\\windows\\installer\\", "\\windows\\systemapps\\",
    "\\windows\\regedit.exe", "\\windows\\explorer.exe",
)
GAME_PATH_MARKERS = (
    "\\games\\", "\\steamapps\\common\\", "\\gog games\\",
    "\\xboxgames\\", "\\epic games\\", "\\riot games\\",
    "\\ubisoft game launcher\\games\\", "\\ea games\\",
)
GAME_PUBLISHER_MARKERS = (
    "valve", "ubisoft", "electronic arts", "ea games", "rockstar games",
    "bethesda", "bandai namco", "square enix", "capcom", "sega",
    "activision", "blizzard", "cd projekt", "riot games", "2k games",
    "take two", "paradox interactive", "warner bros games", "fromsoftware",
)
KNOWN_LAUNCHER_MARKERS = (
    "steam", "epic games launcher", "gog galaxy", "ubisoft connect",
    "ea app", "battle net", "rockstar games launcher", "xbox app",
    "riot client", "unreal editor", "unity editor", "unity hub",
    "playnite", "launchbox", "bluestacks", "faceit", "discord",
)
APP_MARKERS = (
    " browser ", " editor ", " office ", " studio ", " manager ",
    " player ", " client ", " terminal ", " powershell ", " calculator ",
    " recorder ", " viewer ", " launcher ", " control panel ", " utility ",
    " tools ", " desktop ", " word ", " excel ", " powerpoint ",
    " outlook ", " firefox ", " chrome ", " edge ", " blender ",
)


def normalize_scan_text(*values):
    text = " ".join(str(value or "") for value in values).casefold()
    return " " + " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    ) + " "


def scan_phrase_present(text, phrases):
    return any(phrase in text for phrase in phrases)


@lru_cache(maxsize=2048)
def get_executable_identity(path):
    """Read Windows version-resource identity without executing the file."""
    path = clean_path(path)
    if os.name != "nt" or not path or not os.path.isfile(path):
        return {}
    fields = {
        "CompanyName": "publisher",
        "ProductName": "product",
        "FileDescription": "description",
        "OriginalFilename": "original_filename",
        "InternalName": "internal_name",
    }
    result = {}
    try:
        version_api = ctypes.windll.version
        handle = wintypes.DWORD(0)
        size = version_api.GetFileVersionInfoSizeW(path, ctypes.byref(handle))
        if not size:
            return {}
        buffer = ctypes.create_string_buffer(size)
        if not version_api.GetFileVersionInfoW(path, 0, size, buffer):
            return {}

        translations = []
        value_pointer = ctypes.c_void_p()
        value_length = wintypes.UINT(0)
        if version_api.VerQueryValueW(
            buffer, r"\VarFileInfo\Translation",
            ctypes.byref(value_pointer), ctypes.byref(value_length),
        ) and value_length.value >= 4:
            words = ctypes.cast(value_pointer, ctypes.POINTER(ctypes.c_ushort))
            for index in range(0, value_length.value // 2 - 1, 2):
                translations.append((words[index], words[index + 1]))
        translations.extend(((0x0409, 0x04B0), (0x0409, 0x04E4)))

        seen_translations = set()
        for language, codepage in translations:
            if (language, codepage) in seen_translations:
                continue
            seen_translations.add((language, codepage))
            block = f"{language:04x}{codepage:04x}"
            for resource_name, identity_name in fields.items():
                if identity_name in result:
                    continue
                value_pointer = ctypes.c_void_p()
                value_length = wintypes.UINT(0)
                query = f"\\StringFileInfo\\{block}\\{resource_name}"
                if version_api.VerQueryValueW(
                    buffer, query,
                    ctypes.byref(value_pointer), ctypes.byref(value_length),
                ) and value_pointer.value and value_length.value:
                    value = ctypes.wstring_at(value_pointer.value).strip()
                    if value:
                        result[identity_name] = value[:512]
    except (AttributeError, OSError, ValueError, TypeError) as exc:
        LOG.debug("Could not read executable identity for %s: %s", path, exc)
    return result


def game_artifact_score(path):
    """Return a bounded score for well-known game-engine files beside an EXE."""
    if os.name != "nt" or not os.path.isfile(path):
        return 0
    directory = ntpath.dirname(path)
    try:
        names = {name.casefold() for name in os.listdir(directory)[:500]}
    except (OSError, TypeError):
        return 0
    score = 0
    if "unityplayer.dll" in names or "gameassembly.dll" in names:
        score += 5
    if any(name.startswith("steam_api") and name.endswith(".dll") for name in names):
        score += 5
    if any(name.startswith("goggame-") and name.endswith(".dll") for name in names):
        score += 4
    if any(name.endswith("-shipping.exe") for name in names):
        score += 4
    return min(score, 8)


def enrich_scan_item(item, source=None):
    enriched = dict(item)
    path = extract_launch_path(enriched.get("path", ""), require_exists=False)
    enriched["path"] = path
    identity = normalize_identity(enriched.get("identity"))
    for field in ("publisher", "product", "description", "original_filename", "internal_name"):
        value = str(enriched.get(field, "") or "").strip()
        if value and field not in identity:
            identity[field] = value[:512]
    if source:
        identity["source"] = source
    elif enriched.get("source"):
        identity["source"] = str(enriched["source"])[:512]
    if enriched.get("start_menu_group"):
        identity["start_menu_group"] = str(enriched["start_menu_group"])[:512]
    file_identity = get_executable_identity(path)
    for field, value in file_identity.items():
        if value:
            identity[field] = value
    if not identity.get("product"):
        identity["product"] = str(enriched.get("name", "") or "")[:512]
    if not identity.get("original_filename") and path:
        identity["original_filename"] = ntpath.basename(path)
    enriched["identity"] = identity
    enriched["_game_artifacts"] = game_artifact_score(path)
    return enriched


def classify_scan_item(item):
    """Classify one executable locally; no file is launched or deleted."""
    enriched = enrich_scan_item(item)
    identity = enriched.get("identity", {})
    path = clean_path(enriched.get("path", ""))
    path_lower = path.replace("/", "\\").casefold()
    basename = ntpath.basename(path).casefold()
    name_text = normalize_scan_text(
        enriched.get("name"), identity.get("product"),
        identity.get("description"), identity.get("original_filename"),
    )
    all_text = normalize_scan_text(
        name_text, path, identity.get("publisher"), identity.get("start_menu_group"),
    )
    game_score = int(enriched.get("_game_artifacts", 0) or 0)
    app_score = 0
    reasons = []

    if ntpath.splitext(path)[1].lower() not in (".exe", ".bat"):
        kind, confidence = "ignore", 1.0
        reasons.append("unsupported launcher type")
    elif (
        scan_phrase_present(all_text, DRIVER_MARKERS)
        and not any(marker in path_lower for marker in GAME_PATH_MARKERS)
    ):
        kind, confidence = "driver", 0.98
        reasons.append("driver or firmware metadata")
    elif basename in UNNECESSARY_BASENAMES or basename.startswith("unins") or scan_phrase_present(name_text, UNNECESSARY_MARKERS):
        kind, confidence = "ignore", 0.99
        reasons.append("installer, updater, helper, or uninstaller")
    else:
        for marker in GAME_PATH_MARKERS:
            if marker in path_lower:
                game_score += 8
                reasons.append("game-library path")
                break
        group_text = normalize_scan_text(identity.get("start_menu_group"))
        if " games " in group_text:
            game_score += 5
            reasons.append("Games Start Menu group")
        publisher_text = normalize_scan_text(identity.get("publisher"))
        if any(marker in publisher_text for marker in GAME_PUBLISHER_MARKERS):
            game_score += 6
            reasons.append("game publisher metadata")
        if scan_phrase_present(name_text, (" game ", " gameplay ", " dedicated server ")):
            game_score += 2
        if enriched.get("_game_artifacts"):
            reasons.append("game-engine files detected")

        if scan_phrase_present(name_text, tuple(f" {value} " for value in KNOWN_LAUNCHER_MARKERS)):
            kind, confidence = "app", 0.98
            reasons = ["known game-platform application"]
        elif any(marker in path_lower for marker in SYSTEM_PATH_MARKERS):
            kind, confidence = "system", 0.99
            reasons = ["protected Windows system location"]
        else:
            source_name = identity.get("source", "")
            if "\\program files" in path_lower or "\\appdata\\local\\programs\\" in path_lower:
                app_score += 4
            if "registry" in source_name or "start_menu" in source_name:
                app_score += 2
            if scan_phrase_present(name_text, APP_MARKERS):
                app_score += 3
                reasons.append("application metadata")
            if path and basename not in UNNECESSARY_BASENAMES:
                app_score += 1

            strong_game_signal = any(
                reason in reasons for reason in (
                    "game-library path", "Games Start Menu group",
                    "game publisher metadata", "game-engine files detected",
                )
            )
            if game_score >= 6 and (game_score > app_score + 1 or strong_game_signal):
                kind = "game"
                confidence = min(0.99, 0.58 + game_score * 0.045)
            elif app_score >= 4:
                kind = "app"
                confidence = min(0.97, 0.56 + app_score * 0.055)
                if not reasons:
                    reasons.append("installed user application")
            else:
                kind, confidence = "unknown", 0.45
                if not reasons:
                    reasons.append("insufficient metadata; retained for review")

    identity["kind"] = kind
    identity["confidence"] = round(confidence, 3)
    enriched["identity"] = identity
    enriched["kind"] = kind
    enriched["confidence"] = confidence
    enriched["classification_reasons"] = reasons
    return enriched


def extract_launch_path(raw_value, require_exists=True):
    """Extract an executable from a shortcut/DisplayIcon value."""
    value = os.path.expandvars(str(raw_value or "").strip())
    if not value:
        return ""
    if value.startswith('"'):
        closing_quote = value.find('"', 1)
        value = value[1:closing_quote] if closing_quote > 1 else value.strip('"')
    else:
        # DisplayIcon commonly ends in an icon-resource index such as ,0.
        value = value.rsplit(",", 1)[0].strip()
    value = clean_path(value)
    if ntpath.splitext(value)[1].lower() not in (".exe", ".bat"):
        return ""
    if require_exists and not os.path.isfile(value):
        return ""
    return value


def remove_duplicates(items):
    by_path = {}
    result = []
    for raw_item in items:
        path = extract_launch_path(raw_item.get("path", ""), require_exists=False)
        key = canonical_path(path)
        if not key:
            continue
        item = dict(raw_item)
        item["name"] = str(item.get("name", "")).strip() or ntpath.basename(path)
        item["path"] = path
        existing = by_path.get(key)
        if existing is None:
            by_path[key] = item
            result.append(item)
            continue
        for field in (
            "publisher", "product", "description", "original_filename",
            "internal_name", "start_menu_group", "install_location",
            "expected_kind", "old_path",
        ):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]
        sources = {value for value in (existing.get("source"), item.get("source")) if value}
        if sources:
            existing["source"] = "+".join(sorted(sources))
    return result


def identity_words(value):
    return {
        word for word in normalize_scan_text(value).split()
        if len(word) >= 3 and word not in {
            "the", "and", "for", "app", "game", "launcher", "client",
            "edition", "windows", "program", "application",
        }
    }


def find_likely_executable(directory, display_name, publisher="", max_files=80):
    """Conservatively locate a renamed main EXE using immutable version metadata."""
    directory = clean_path(directory)
    if os.name != "nt" or not os.path.isdir(directory):
        return ""
    candidates = []
    base_depth = directory.rstrip("\\/").count(os.sep)
    skipped_directories = {
        "redist", "redistributable", "installer", "uninstall", "temp", "tmp",
        "crashreporter", "crashpad", "plugins", "resources", "locales",
    }
    try:
        for root_dir, dirs, files in os.walk(directory):
            depth = root_dir.rstrip("\\/").count(os.sep) - base_depth
            dirs[:] = [folder for folder in dirs if folder.casefold() not in skipped_directories]
            if depth >= 4:
                dirs[:] = []
            for filename in files:
                if len(candidates) >= max_files:
                    break
                if not filename.lower().endswith(".exe"):
                    continue
                if filename.casefold() in UNNECESSARY_BASENAMES or filename.casefold().startswith("unins"):
                    continue
                candidates.append(os.path.join(root_dir, filename))
            if len(candidates) >= max_files:
                break
    except OSError:
        return ""
    if not candidates:
        return ""

    wanted_words = identity_words(display_name)
    publisher_text = normalize_scan_text(publisher)

    # Rank cheaply first, then read version resources for only the strongest
    # candidates. This keeps large game folders from making scans stall.
    preliminary = []
    for candidate in candidates:
        stem = ntpath.splitext(ntpath.basename(candidate))[0]
        stem_words = identity_words(stem)
        score = 0
        if normalize_scan_text(stem) == normalize_scan_text(display_name):
            score += 10
        if wanted_words:
            score += int(5 * len(wanted_words & stem_words) / len(wanted_words))
        if ntpath.dirname(candidate).casefold() == directory.casefold():
            score += 2
        try:
            size = os.path.getsize(candidate)
            if size >= 20 * 1024 * 1024:
                score += 2
            elif size >= 2 * 1024 * 1024:
                score += 1
        except OSError:
            pass
        preliminary.append((score, candidate))
    preliminary.sort(key=lambda entry: entry[0], reverse=True)

    ranked = []
    for preliminary_score, candidate in preliminary[:18]:
        stem = ntpath.splitext(ntpath.basename(candidate))[0]
        stem_words = identity_words(stem)
        metadata = get_executable_identity(candidate)
        product = metadata.get("product") or metadata.get("description") or ""
        product_words = identity_words(product)
        score = preliminary_score
        if product and normalize_scan_text(product) == normalize_scan_text(display_name):
            score += 12
        if wanted_words:
            score += int(7 * len(wanted_words & product_words) / len(wanted_words))
        company = normalize_scan_text(metadata.get("publisher"))
        if publisher_text.strip() and company == publisher_text:
            score += 4
        candidate_text = normalize_scan_text(stem, product)
        if scan_phrase_present(candidate_text, UNNECESSARY_MARKERS):
            score -= 12
        ranked.append((score, candidate))

    ranked.sort(key=lambda entry: entry[0], reverse=True)
    best_score, best_path = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else -99
    if best_score >= 7 and best_score >= second_score + 2:
        return best_path
    if len(ranked) == 1 and best_score >= 2:
        return best_path
    return ""


def scan_broken_library_paths():
    """Look beside missing library paths for EXEs renamed after being added."""
    if os.name != "nt":
        return []
    with data_lock:
        snapshots = [
            ("game", item.get("name", ""), item.get("path", ""),
             dict(item.get("identity", {})))
            for item in games
        ] + [
            ("app", item.get("name", ""), item.get("path", ""),
             dict(item.get("identity", {})))
            for item in apps
        ]
    recovered_candidates = []
    for expected_kind, name, old_path, identity in snapshots:
        if scan_cancel:
            break
        old_path = clean_path(old_path)
        if not old_path or os.path.isfile(old_path):
            continue
        parent = ntpath.dirname(old_path)
        candidate = find_likely_executable(parent, name, identity.get("publisher", ""))
        if not candidate:
            continue
        recovered_candidates.append({
            "name": name,
            "path": candidate,
            "publisher": identity.get("publisher", ""),
            "product": identity.get("product", name),
            "source": "recovery",
            "expected_kind": expected_kind,
            "old_path": old_path,
        })
    return recovered_candidates


def scan_start_menu():
    found = []
    paths = [
        os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            r"Microsoft\Windows\Start Menu\Programs",
        ),
        os.path.join(
            os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming")),
            r"Microsoft\Windows\Start Menu\Programs",
        ),
    ]
    for base in paths:
        if not os.path.isdir(base):
            continue
        for root_dir, _dirs, files in os.walk(base):
            if scan_cancel:
                return found
            for filename in files:
                if not filename.lower().endswith(".lnk"):
                    continue
                shortcut_path = os.path.join(root_dir, filename)
                target = extract_launch_path(resolve_shortcut(shortcut_path))
                if target:
                    relative_group = os.path.relpath(root_dir, base)
                    start_menu_group = "" if relative_group == "." else relative_group.split(os.sep, 1)[0]
                    found.append({
                        "name": os.path.splitext(filename)[0],
                        "path": target,
                        "source": "start_menu",
                        "start_menu_group": start_menu_group,
                    })
    return found


def _registry_value(key, name, default=""):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def scan_registry():
    if not HAS_WINREG:
        return []
    found = []
    key_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    for hive in hives:
        for key_path in key_paths:
            if scan_cancel:
                return found
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        if scan_cancel:
                            return found
                        try:
                            subkey_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                name = str(_registry_value(subkey, "DisplayName", "")).strip()
                                system_component = str(
                                    _registry_value(subkey, "SystemComponent", 0) or 0
                                ).strip()
                                if not name or system_component == "1":
                                    continue
                                publisher = str(_registry_value(subkey, "Publisher", "")).strip()
                                install_location = clean_path(
                                    str(_registry_value(subkey, "InstallLocation", "")).strip()
                                )
                                target = extract_launch_path(
                                    _registry_value(subkey, "DisplayIcon", "")
                                )
                                if not target and install_location:
                                    target = find_likely_executable(
                                        install_location, name, publisher
                                    )
                                if name and target:
                                    found.append({
                                        "name": name,
                                        "path": target,
                                        "publisher": publisher,
                                        "product": name,
                                        "install_location": install_location,
                                        "source": "registry",
                                    })
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def scan_window_exists():
    try:
        return scan_window is not None and bool(scan_window.winfo_exists())
    except (NameError, tk.TclError):
        return False


def request_scan_cancel(close_window=False):
    global scan_cancel
    scan_cancel = True
    if scan_window_exists():
        scan_status_lbl.configure(text="CANCELLING...", fg=ORANGE)
        if close_window:
            scan_window.withdraw()


def create_scan_window():
    global scan_window, scan_count_lbl, scan_name_lbl, scan_progress, preview_box, scan_status_lbl
    global scan_breakdown_lbl, scan_progress_value
    scan_window = tk.Toplevel(root)
    scan_window.title("System Scanner")
    scan_window.geometry("820x590")
    scan_window.minsize(700, 510)
    scan_window.configure(bg=BG)
    scan_window.transient(root)
    scan_window.grab_set()
    scan_window.protocol("WM_DELETE_WINDOW", lambda: request_scan_cancel(True))

    tk.Frame(scan_window, bg=ACCENT, height=4).pack(fill="x")
    tk.Label(
        scan_window, text="SYSTEM DISCOVERY", bg=BG, fg=TEXT,
        font=("Segoe UI Black", 25, "bold"),
    ).pack(pady=(20, 2))
    tk.Label(
        scan_window,
        text="LOCAL INTELLIGENCE  //  GAMES · APPS · DRIVERS · SAFE CLEANUP",
        bg=BG, fg=NEON, font=("Segoe UI", 9, "bold"),
    ).pack(pady=(0, 10))
    scan_status_lbl = tk.Label(
        scan_window, text="PREPARING...", bg=BG, fg=GREEN,
        font=("Segoe UI", 14, "bold"),
    )
    scan_status_lbl.pack(pady=5)
    scan_count_lbl = tk.Label(
        scan_window, text="FOUND: 0", bg=BG, fg=TEXT,
        font=("Segoe UI", 20, "bold"),
    )
    scan_count_lbl.pack(pady=(8, 2))
    scan_breakdown_lbl = tk.Label(
        scan_window,
        text="GAMES 0   ·   APPS 0   ·   REVIEW 0   ·   FILTERED 0   ·   RECOVERED 0",
        bg=BG, fg=SUBTEXT, font=("Consolas", 9, "bold"),
    )
    scan_breakdown_lbl.pack(pady=(0, 6))

    scan_progress = tk.Canvas(
        scan_window, width=600, height=30, bg=CARD2,
        highlightthickness=2, highlightbackground=ACCENT,
    )
    scan_progress.pack(pady=12)
    scan_progress.create_rectangle(0, 0, 600, 30, fill=CARD2, outline="", tags="progress-bg")
    scan_progress.create_rectangle(0, 0, 0, 30, fill=ACCENT, outline="", tags="progress-fill")
    scan_progress.create_text(300, 15, text="0%", fill=TEXT, font=("Segoe UI", 11, "bold"), tags="progress-text")
    scan_progress_value = 0.0
    scan_name_lbl = tk.Label(
        scan_window, text="Starting scan...", bg=BG, fg=SUBTEXT,
        font=("Consolas", 11),
    )
    scan_name_lbl.pack(pady=6)

    preview_frame = tk.Frame(scan_window, bg=BG)
    preview_frame.pack(fill="both", expand=True, padx=30, pady=10)
    tk.Label(
        preview_frame, text="RECENT SIGNALS", bg=BG, fg=NEON,
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 5))
    preview_box = tk.Listbox(
        preview_frame, bg=CARD, fg=TEXT, font=("Consolas", 10),
        selectbackground=ACCENT, selectforeground=TEXT,
        highlightbackground=ACCENT, highlightthickness=1,
    )
    preview_box.pack(fill="both", expand=True)
    cancel_button = tk.Button(
        scan_window, text="CANCEL", command=request_scan_cancel,
        bg=RED, fg=TEXT, activebackground="#dc2626", activeforeground=TEXT,
        relief="flat", font=("Segoe UI", 10, "bold"), padx=24, pady=8,
    )
    bind_animated_button(cancel_button, RED, "#f87171", TEXT, TEXT)
    cancel_button.pack(pady=(4, 16))
    fade_window(scan_window, 0.0, 1.0, 220)


def set_scan_phase(text):
    if scan_window_exists():
        scan_status_lbl.configure(text=text)
        animate_widget_color(scan_status_lbl, "fg", GREEN, 180, 9, GREEN)
        scan_name_lbl.configure(text=text.title())


def animate_scan_progress(target_percent):
    if not scan_window_exists():
        return
    start = scan_progress_value
    target = max(0.0, min(100.0, float(target_percent)))

    def update(progress):
        global scan_progress_value
        scan_progress_value = start + (target - start) * progress
        scan_progress.coords("progress-fill", 0, 0, 6 * scan_progress_value, 30)
        scan_progress.itemconfigure("progress-text", text=f"{int(round(scan_progress_value))}%")

    start_animation(scan_progress, "scan-progress", 190, 11, update, easing=ease_in_out_cubic)


def update_scan_progress(current, total, item_name="", kind="", breakdown=None):
    if not scan_window_exists():
        return
    percent = 100 if total == 0 else min(100, int(current * 100 / total))
    animate_scan_progress(percent)
    scan_count_lbl.configure(text=f"ANALYZED: {current} / {total}")
    scan_name_lbl.configure(text=item_name or f"Processed {current} of {total}")
    if breakdown:
        filtered = sum(breakdown.get(value, 0) for value in ("driver", "system", "ignore"))
        scan_breakdown_lbl.configure(
            text=(
                f"GAMES {breakdown.get('game', 0)}   ·   "
                f"APPS {breakdown.get('app', 0)}   ·   "
                f"REVIEW {breakdown.get('unknown', 0)}   ·   "
                f"FILTERED {filtered}   ·   "
                f"RECOVERED {breakdown.get('recovery', 0)}"
            )
        )
    if item_name:
        labels = {
            "game": ("GAME", GREEN),
            "app": ("APP", CYAN),
            "unknown": ("REVIEW", ORANGE),
            "driver": ("DRIVER · FILTERED", MUTED),
            "system": ("SYSTEM · FILTERED", MUTED),
            "ignore": ("IRRELEVANT · FILTERED", MUTED),
        }
        label, foreground = labels.get(kind, ("FOUND", TEXT))
        preview_box.insert(0, f"{label:<20} {item_name}")
        try:
            preview_box.itemconfig(0, fg=foreground)
        except tk.TclError:
            pass
        if preview_box.size() > 15:
            preview_box.delete(15, tk.END)


def scanned_library_item(scanned):
    identity = normalize_identity(scanned.get("identity"))
    return {
        "name": str(scanned.get("name", "")).strip() or ntpath.basename(scanned.get("path", "")),
        "path": clean_path(scanned.get("path", "")),
        "trainer": "",
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color(),
        "identity": identity,
    }


def identity_match_score(existing, candidate):
    """Score a candidate for conservative renamed-launcher recovery."""
    old_path = clean_path(existing.get("path", ""))
    new_path = clean_path(candidate.get("path", ""))
    if not old_path or not new_path:
        return 0
    if canonical_path(old_path) == canonical_path(new_path):
        return 100
    existing_identity = normalize_identity(existing.get("identity"))
    candidate_identity = normalize_identity(candidate.get("identity"))
    score = 0

    old_parent = canonical_path(ntpath.dirname(old_path))
    new_parent = canonical_path(ntpath.dirname(new_path))
    if old_parent and old_parent == new_parent:
        score += 9

    existing_name = normalize_scan_text(existing.get("name"))
    candidate_names = {
        normalize_scan_text(candidate.get("name")),
        normalize_scan_text(candidate_identity.get("product")),
        normalize_scan_text(candidate_identity.get("description")),
    }
    if existing_name.strip() and existing_name in candidate_names:
        score += 10
    existing_words = identity_words(existing.get("name"))
    candidate_words = set()
    for value in (
        candidate.get("name"), candidate_identity.get("product"),
        candidate_identity.get("description"),
    ):
        candidate_words.update(identity_words(value))
    if existing_words:
        score += int(6 * len(existing_words & candidate_words) / len(existing_words))

    old_original = normalize_scan_text(existing_identity.get("original_filename"))
    new_original = normalize_scan_text(candidate_identity.get("original_filename"))
    if old_original.strip() and old_original == new_original:
        score += 10
    old_product = normalize_scan_text(existing_identity.get("product"))
    new_product = normalize_scan_text(candidate_identity.get("product"))
    if old_product.strip() and old_product == new_product:
        score += 9
    old_publisher = normalize_scan_text(existing_identity.get("publisher"))
    new_publisher = normalize_scan_text(candidate_identity.get("publisher"))
    if old_publisher.strip() and old_publisher == new_publisher:
        score += 3
    return score


def recover_renamed_entries(classified_items):
    used_paths = set()
    changed_libraries = set()
    recovered_items = []
    candidates = [
        item for item in classified_items
        if item.get("kind") in ("game", "app") or "recovery" in str(item.get("source", ""))
    ]
    with data_lock:
        for library_name, library, expected_kind in (
            ("games", games, "game"), ("apps", apps, "app")
        ):
            for existing in library:
                old_path = clean_path(existing.get("path", ""))
                if not old_path or os.path.isfile(old_path):
                    continue
                ranked = []
                for candidate in candidates:
                    new_key = canonical_path(candidate.get("path", ""))
                    if not new_key or new_key in used_paths:
                        continue
                    candidate_kind = candidate.get("expected_kind") or candidate.get("kind")
                    if candidate_kind != expected_kind:
                        continue
                    score = identity_match_score(existing, candidate)
                    if score:
                        ranked.append((score, candidate))
                if not ranked:
                    continue
                ranked.sort(key=lambda entry: entry[0], reverse=True)
                best_score, best = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else -99
                if best_score < 9 or best_score < second_score + 2:
                    continue
                existing["path"] = clean_path(best["path"])
                existing["identity"] = normalize_identity(best.get("identity"))
                used_paths.add(canonical_path(best["path"]))
                changed_libraries.add(library_name)
                recovered_items.append(existing)
    return used_paths, changed_libraries, recovered_items


def prepare_founded_classifications():
    with data_lock:
        snapshots = [dict(item) for item in founded]
    return {
        canonical_path(item.get("path", "")): classify_scan_item(item)
        for item in snapshots if item.get("path")
    }


def apply_intelligent_scan_results(items, existing_classifications):
    """Route safe results, clean scanner-only noise, and repair renamed paths."""
    summary = {
        "game": 0, "app": 0, "unknown": 0, "filtered": 0,
        "recovered": 0, "moved": 0,
    }
    used_recoveries, changed_libraries, recovered_items = recover_renamed_entries(items)
    summary["recovered"] = len(recovered_items)
    new_icon_items = list(recovered_items)

    with data_lock:
        path_sets = {
            "games": {canonical_path(item.get("path", "")) for item in games},
            "apps": {canonical_path(item.get("path", "")) for item in apps},
            "founded": {canonical_path(item.get("path", "")) for item in founded},
        }

        # Reclassify old scanner results so the Discovered tab becomes a true
        # review queue instead of accumulating installers, drivers, and tools.
        for existing in list(founded):
            key = canonical_path(existing.get("path", ""))
            classified = existing_classifications.get(key) or classify_scan_item(existing)
            kind = classified.get("kind", "unknown")
            existing["identity"] = normalize_identity(classified.get("identity"))
            if kind in ("game", "app"):
                target_name = "games" if kind == "game" else "apps"
                target = games if kind == "game" else apps
                founded.remove(existing)
                path_sets["founded"].discard(key)
                changed_libraries.add("founded")
                if key not in path_sets[target_name]:
                    target.append(existing)
                    path_sets[target_name].add(key)
                    changed_libraries.add(target_name)
                    new_icon_items.append(existing)
                    summary[kind] += 1
                summary["moved"] += 1
            elif kind in ("driver", "system", "ignore"):
                founded.remove(existing)
                path_sets["founded"].discard(key)
                changed_libraries.add("founded")
                summary["filtered"] += 1
            else:
                summary["unknown"] += 1
                changed_libraries.add("founded")

        all_paths = path_sets["games"] | path_sets["apps"] | path_sets["founded"]
        for classified in items:
            key = canonical_path(classified.get("path", ""))
            if not key or key in all_paths or key in used_recoveries:
                continue
            kind = classified.get("kind", "unknown")
            if kind in ("driver", "system", "ignore"):
                summary["filtered"] += 1
                continue
            entry = scanned_library_item(classified)
            if kind == "game":
                games.append(entry)
                changed_libraries.add("games")
                summary["game"] += 1
                new_icon_items.append(entry)
            elif kind == "app":
                apps.append(entry)
                changed_libraries.add("apps")
                summary["app"] += 1
                new_icon_items.append(entry)
            else:
                founded.append(entry)
                changed_libraries.add("founded")
                summary["unknown"] += 1
            all_paths.add(key)

    if "games" in changed_libraries:
        _save(GAMES_FILE, games)
    if "apps" in changed_libraries:
        _save(APPS_FILE, apps)
    if "founded" in changed_libraries:
        _save(FOUNDED_FILE, founded)

    # Icon work stays asynchronous and is limited to newly routed entries.
    for item in new_icon_items[:24]:
        icon_path = os.path.join(ICONS_DIR, item.get("icon", "")) if item.get("icon") else ""
        if item.get("path") and (not icon_path or not os.path.isfile(icon_path)):
            extract_icon_in_background(item, item["path"])
    return summary


def set_scan_button_running(running):
    scan_btn.configure(state="disabled" if running else "normal")
    target = lerp_color(GREEN, CARD2, 0.65) if running else GREEN
    animate_widget_color(scan_btn, "bg", target, 200, 10, GREEN)
    animate_widget_color(scan_btn, "fg", SUBTEXT if running else TEXT, 200, 10, TEXT)


def finish_scan(items, existing_classifications):
    global scan_running, all_scanned_items
    if scan_cancel:
        cancel_scan_ui()
        return
    all_scanned_items = items
    summary = apply_intelligent_scan_results(items, existing_classifications)
    scan_running = False
    set_scan_button_running(False)
    pulse_widget(scan_btn, GREEN, GREEN_HOVER, cycles=1, duration=180)
    if scan_window_exists():
        breakdown = {kind: 0 for kind in ("game", "app", "unknown", "driver", "system", "ignore")}
        for item in items:
            kind = item.get("kind", "unknown")
            breakdown[kind] = breakdown.get(kind, 0) + 1
        breakdown["recovery"] = summary["recovered"]
        update_scan_progress(len(items), len(items), breakdown=breakdown)
        scan_status_lbl.configure(
            text=(
                f"COMPLETE — {summary['game']} GAMES · {summary['app']} APPS · "
                f"{summary['unknown']} REVIEW · {summary['filtered']} FILTERED · "
                f"{summary['recovered']} REPAIRED"
            ),
            fg=GREEN,
        )
        scan_name_lbl.configure(text="Opening Discovered review queue...")
        root.after(1400, close_scan_window, True)
    else:
        switch_tab("founded")


def close_scan_window(open_founded=False):
    global scan_window
    if not scan_window_exists():
        scan_window = None
        if open_founded:
            switch_tab("founded")
        return
    window = scan_window

    def finalize():
        global scan_window
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            pass
        if scan_window is window:
            scan_window = None
        if open_founded:
            switch_tab("founded")

    fade_window(window, 1.0, 0.0, 160, finalize)


def cancel_scan_ui():
    global scan_running
    scan_running = False
    set_scan_button_running(False)
    if scan_window_exists():
        scan_status_lbl.configure(text="SCAN CANCELLED", fg=ORANGE)
        root.after(500, close_scan_window)


def fail_scan_ui(message):
    global scan_running
    scan_running = False
    set_scan_button_running(False)
    if scan_window_exists():
        scan_status_lbl.configure(text="SCAN FAILED", fg=RED)
        scan_name_lbl.configure(text=message)
    messagebox.showerror("Scanner Error", message)


def scan_all_programs():
    com_initialized = False
    try:
        if HAS_WIN32COM:
            pythoncom.CoInitialize()
            com_initialized = True
        post_ui(set_scan_phase, "SCANNING START MENU...")
        start_menu_items = scan_start_menu()
        if scan_cancel:
            post_ui(cancel_scan_ui)
            return
        post_ui(set_scan_phase, "SCANNING REGISTRY METADATA...")
        registry_items = scan_registry()
        if scan_cancel:
            post_ui(cancel_scan_ui)
            return
        post_ui(set_scan_phase, "RECOVERING RENAMED LAUNCHERS...")
        recovery_items = scan_broken_library_paths()
        if scan_cancel:
            post_ui(cancel_scan_ui)
            return

        raw_items = remove_duplicates(start_menu_items + registry_items + recovery_items)
        classified_items = []
        breakdown = {kind: 0 for kind in ("game", "app", "unknown", "driver", "system", "ignore")}
        breakdown["recovery"] = 0
        total = len(raw_items)
        progress_step = max(1, total // 50)
        post_ui(set_scan_phase, "CLASSIFYING LOCAL SOFTWARE...")
        for index, raw_item in enumerate(raw_items, 1):
            if scan_cancel:
                post_ui(cancel_scan_ui)
                return
            item = classify_scan_item(raw_item)
            classified_items.append(item)
            kind = item.get("kind", "unknown")
            breakdown[kind] = breakdown.get(kind, 0) + 1
            if "recovery" in str(raw_item.get("source", "")):
                breakdown["recovery"] += 1
            if index == total or index % progress_step == 0:
                post_ui(
                    update_scan_progress, index, total, item["name"], kind,
                    dict(breakdown),
                )

        post_ui(set_scan_phase, "RECLASSIFYING DISCOVERED ITEMS...")
        existing_classifications = prepare_founded_classifications()
        if scan_cancel:
            post_ui(cancel_scan_ui)
            return
        post_ui(finish_scan, classified_items, existing_classifications)
    except Exception as exc:
        LOG.exception("System scan failed")
        post_ui(fail_scan_ui, str(exc))
    finally:
        if hasattr(shortcut_com_state, "shell"):
            del shortcut_com_state.shell
        if com_initialized:
            pythoncom.CoUninitialize()


def start_auto_scan():
    global scan_cancel, scan_running
    if scan_running:
        if scan_window_exists():
            scan_window.deiconify()
            scan_window.lift()
        return
    scan_cancel = False
    scan_running = True
    create_scan_window()
    set_scan_button_running(True)
    threading.Thread(target=scan_all_programs, daemon=True, name="system-scanner").start()

# ==========================================
# ITEM MANAGEMENT
# ==========================================
def library_has_path(library, path, exclude=None):
    key = canonical_path(path)
    return any(
        candidate is not exclude and canonical_path(candidate.get("path", "")) == key
        for candidate in library
    )


def move_to_games(item):
    if item not in founded:
        return
    if library_has_path(games, item.get("path", "")):
        messagebox.showwarning("Duplicate", "This executable already exists in Games.")
        return
    with data_lock:
        founded.remove(item)
        games.append(item)
    _save(FOUNDED_FILE, founded)
    _save(GAMES_FILE, games)
    refresh()


def move_to_apps(item):
    if item not in founded:
        return
    if library_has_path(apps, item.get("path", "")):
        messagebox.showwarning("Duplicate", "This executable already exists in Workspace.")
        return
    with data_lock:
        founded.remove(item)
        apps.append(item)
    _save(FOUNDED_FILE, founded)
    _save(APPS_FILE, apps)
    refresh()

def sort_items(lst):
    if current_sort == "name":
        return sorted(lst, key=lambda g: g["name"].lower())
    elif current_sort == "playtime":
        return sorted(lst, key=lambda g: g["playtime"], reverse=True)
    else:
        return sorted(lst, key=lambda g: (not g["pinned"], g["name"].lower()))

def toggle_pin(item):
    with data_lock:
        item["pinned"] = not item["pinned"]
    save_current()
    refresh()

def delete_item(item):
    if messagebox.askyesno("Delete", f"Delete '{item['name']}'?"):
        with data_lock:
            if item not in current_list():
                return
            current_list().remove(item)
        save_current()
        refresh()

def move_item(item):
    source = current_list()
    target = apps if active_tab == "games" else games
    target_name = "Workspace" if active_tab == "games" else "Games"
    if library_has_path(target, item.get("path", "")):
        messagebox.showwarning("Duplicate", f"This executable already exists in {target_name}.")
        return
    if item not in source:
        return
    with data_lock:
        source.remove(item)
        target.append(item)
    if active_tab == "games":
        _save(GAMES_FILE, games)
        _save(APPS_FILE, apps)
    else:
        _save(APPS_FILE if active_tab == "apps" else FOUNDED_FILE, source)
        _save(GAMES_FILE, games)
    refresh()

def rename_item(item):
    new_name = simpledialog.askstring("Rename", "Enter new name:", initialvalue=item["name"])
    if new_name and new_name.strip():
        with data_lock:
            item["name"] = new_name.strip()
        save_current()
        refresh()

def open_file_location(item):
    path = item.get("path", "")
    if os.path.exists(path):
        os.startfile(os.path.dirname(os.path.abspath(path)))
    else:
        messagebox.showerror("Error", "File not found")


def find_path_conflict(path, exclude=None):
    for label, library in (("Games", games), ("Workspace", apps), ("Discovered", founded)):
        if library_has_path(library, path, exclude=exclude):
            return label
    return ""


def refresh_item_identity_in_background(item, path):
    """Refresh stable executable identity after a manual path repair."""
    def worker():
        metadata = get_executable_identity(path)
        if not metadata:
            return
        with data_lock:
            still_present = any(candidate is item for candidate in games + apps + founded)
            if not still_present or canonical_path(item.get("path", "")) != canonical_path(path):
                return
            old_identity = dict(item.get("identity", {}))
            updated_identity = dict(old_identity)
            updated_identity.update(metadata)
            updated_identity["source"] = "manual_location"
            item["identity"] = normalize_identity(updated_identity)
        if not save_item_library(item):
            with data_lock:
                if old_identity:
                    item["identity"] = old_identity
                else:
                    item.pop("identity", None)

    threading.Thread(
        target=worker, daemon=True,
        name=f"identity-refresh-{uuid.uuid4().hex[:8]}",
    ).start()


def change_item_location(item):
    """Repair one launch path while preserving every other user field."""
    if is_item_running(item):
        messagebox.showwarning(
            "Change Location",
            f"End {item.get('name', 'this task')} before changing its launch location.",
        )
        return
    old_path = clean_path(item.get("path", ""))
    selected = filedialog.askopenfilename(
        title=f"Change Location — {item.get('name', 'Launcher')}",
        initialdir=os.path.dirname(old_path) if old_path else BASE_DIR,
        filetypes=[
            ("Launchable files", "*.exe *.bat"),
            ("Executable files", "*.exe"),
            ("Batch files", "*.bat"),
            ("All files", "*.*"),
        ],
    )
    new_path = clean_path(selected)
    if not new_path or canonical_path(new_path) == canonical_path(old_path):
        return
    if ntpath.splitext(new_path)[1].lower() not in (".exe", ".bat"):
        messagebox.showerror("Change Location", "Choose an .exe or .bat launcher.")
        return
    if not os.path.isfile(new_path):
        messagebox.showerror("Change Location", f"The selected file does not exist:\n{new_path}")
        return
    conflict = find_path_conflict(new_path, exclude=item)
    if conflict:
        messagebox.showwarning(
            "Duplicate Location",
            f"That launcher is already registered in {conflict}.",
        )
        return

    old_identity = dict(item.get("identity", {}))
    with data_lock:
        item["path"] = new_path
    if not save_item_library(item):
        with data_lock:
            item["path"] = old_path
            if old_identity:
                item["identity"] = old_identity
        messagebox.showerror("Change Location", "The new location could not be saved.")
        return

    add_activity(
        "location", f"Location updated · {item.get('name', 'Launcher')}", item,
        detail=f"{ntpath.basename(old_path) or 'missing path'}  →  {ntpath.basename(new_path)}",
        severity="info",
    )
    refresh()
    refresh_item_identity_in_background(item, new_path)
    icon_filename = str(item.get("icon", "") or "")
    icon_path = os.path.join(ICONS_DIR, icon_filename) if icon_filename else ""
    if not icon_path or not os.path.isfile(icon_path):
        extract_icon_in_background(item, new_path)


def invalidate_icon_caches(icon_filename):
    if not icon_filename:
        return
    prefix = f"{icon_filename}-"
    for key in [key for key in icon_cache if str(key).startswith(prefix)]:
        icon_cache.pop(key, None)
    icon_path = os.path.abspath(os.path.join(ICONS_DIR, icon_filename))
    for key in [
        key for key in card_art_cache
        if isinstance(key, tuple) and os.path.abspath(str(key[0])) == icon_path
    ]:
        card_art_cache.pop(key, None)


def remove_unreferenced_cached_icon(icon_filename):
    """Delete only an unreferenced file inside XVVIIX's own icon cache."""
    filename = str(icon_filename or "").strip()
    if not filename or os.path.basename(filename) != filename:
        return
    path = os.path.abspath(os.path.join(ICONS_DIR, filename))
    with data_lock:
        referenced = any(
            candidate.get("icon") == filename
            or (
                candidate.get("artwork")
                and os.path.abspath(
                    os.path.expanduser(os.path.expandvars(str(candidate.get("artwork"))))
                    if os.path.isabs(str(candidate.get("artwork")))
                    else os.path.join(BASE_DIR, str(candidate.get("artwork")))
                ) == path
            )
            for candidate in games + apps + founded
        )
    if referenced:
        return
    cache_root = os.path.abspath(ICONS_DIR) + os.sep
    if not path.startswith(cache_root):
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        LOG.debug("Could not remove unused icon %s: %s", filename, exc)
    invalidate_icon_caches(filename)


def build_custom_icon(source_path, destination_path):
    """Convert an image or Windows executable icon into a safe square PNG."""
    if not HAS_PIL:
        raise RuntimeError("Pillow is required for custom icons")
    source_path = clean_path(source_path)
    extension = ntpath.splitext(source_path)[1].lower()
    temporary_ico = ""
    image_path = source_path
    try:
        if extension != ".exe" and os.path.getsize(source_path) > 64 * 1024 * 1024:
            raise RuntimeError("The selected image is larger than the 64 MB safety limit")
        if extension == ".exe":
            if not HAS_ICOEXTRACT:
                raise RuntimeError("EXE icon extraction is unavailable; choose an image or .ico file")
            temporary_ico = f"{destination_path}.source-{uuid.uuid4().hex}.ico"
            IconExtractor(source_path).export_icon(temporary_ico)
            if not os.path.isfile(temporary_ico):
                raise RuntimeError("No icon could be extracted from that executable")
            image_path = temporary_ico
        with Image.open(image_path) as source:
            if source.width * source.height > 40_000_000:
                raise RuntimeError("The selected image exceeds the 40-megapixel safety limit")
            source.load()
            image = source.convert("RGBA")
        if not image.width or not image.height:
            raise RuntimeError("The selected image has no usable pixels")
        target = 240
        scale = min(target / image.width, target / image.height)
        resized = image.resize(
            (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
            Image.LANCZOS,
        )
        output = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        output.alpha_composite(
            resized, ((256 - resized.width) // 2, (256 - resized.height) // 2),
        )
        temporary_png = f"{destination_path}.tmp-{uuid.uuid4().hex}"
        try:
            output.save(temporary_png, "PNG", optimize=True)
            os.replace(temporary_png, destination_path)
        finally:
            if os.path.exists(temporary_png):
                try:
                    os.remove(temporary_png)
                except OSError:
                    pass
    except Exception as exc:
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
    finally:
        if temporary_ico and os.path.exists(temporary_ico):
            try:
                os.remove(temporary_ico)
            except OSError:
                pass


def finish_custom_icon_update(item_name, error=""):
    if error:
        messagebox.showerror("Add Icon", f"Could not update the icon for {item_name}:\n\n{error}")
    refresh()


def add_custom_icon(item):
    if not HAS_PIL:
        messagebox.showerror("Add Icon", "Install Pillow to use custom icons.")
        return
    item_key = id(item)
    with data_lock:
        if item_key in icon_update_pending:
            messagebox.showinfo("Add Icon", "An icon update is already in progress for this card.")
            return
    selected = filedialog.askopenfilename(
        title=f"Add Icon — {item.get('name', 'Launcher')}",
        filetypes=[
            ("Icon and image files", "*.ico *.png *.jpg *.jpeg *.webp *.bmp"),
            ("Windows executable icon", "*.exe"),
            ("All files", "*.*"),
        ],
    )
    selected = clean_path(selected)
    if not selected:
        return
    if not os.path.isfile(selected):
        messagebox.showerror("Add Icon", f"The selected file does not exist:\n{selected}")
        return
    with data_lock:
        icon_update_pending.add(item_key)

    def worker():
        filename = f"custom_{uuid.uuid4().hex}.png"
        destination = os.path.join(ICONS_DIR, filename)
        old_icon = str(item.get("icon", "") or "")
        error = ""
        try:
            build_custom_icon(selected, destination)
            with data_lock:
                still_present = any(candidate is item for candidate in games + apps + founded)
                if not still_present:
                    raise RuntimeError("The card was removed before the icon update finished")
                item["icon"] = filename
            if not save_item_library(item):
                with data_lock:
                    item["icon"] = old_icon
                raise RuntimeError("the library file could not be saved")
            invalidate_icon_caches(old_icon)
            remove_unreferenced_cached_icon(old_icon)
            add_activity(
                "icon", f"Icon updated · {item.get('name', 'Launcher')}", item,
                detail=os.path.basename(selected), severity="success",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            error = str(exc) or exc.__class__.__name__
            try:
                if os.path.isfile(destination):
                    os.remove(destination)
            except OSError:
                pass
        finally:
            with data_lock:
                icon_update_pending.discard(item_key)
        post_ui(finish_custom_icon_update, item.get("name", "Launcher"), error)

    threading.Thread(
        target=worker, daemon=True,
        name=f"custom-icon-{uuid.uuid4().hex[:8]}",
    ).start()


def schedule_icon_refresh():
    global icon_refresh_job
    if root is None:
        return
    if icon_refresh_job is not None:
        try:
            root.after_cancel(icon_refresh_job)
        except tk.TclError:
            pass
    icon_refresh_job = root.after(150, finish_icon_refresh)


def finish_icon_refresh():
    global icon_refresh_job
    icon_refresh_job = None
    refresh()


def extract_icon_in_background(item, path):
    if not HAS_PIL:
        return

    def worker():
        icon_filename = ""
        try:
            if path.lower().endswith(".bat"):
                icon_filename = f"icon_bat_{uuid.uuid4().hex}.png"
                Image.new("RGBA", (64, 64), (40, 40, 40, 255)).save(
                    os.path.join(ICONS_DIR, icon_filename), "PNG"
                )
            elif HAS_ICOEXTRACT:
                icon_filename = f"icon_{uuid.uuid4().hex}.png"
                temporary_ico = os.path.join(ICONS_DIR, icon_filename.replace(".png", ".ico"))
                output_png = os.path.join(ICONS_DIR, icon_filename)
                IconExtractor(path).export_icon(temporary_ico)
                if os.path.exists(temporary_ico):
                    with Image.open(temporary_ico) as image:
                        image.save(output_png, "PNG")
                    os.remove(temporary_ico)
                else:
                    icon_filename = ""
        except (OSError, ValueError) as exc:
            LOG.warning("Could not extract icon from %s: %s", path, exc)
            icon_filename = ""

        if not icon_filename:
            return
        with data_lock:
            still_present = any(
                candidate is item for candidate in games + apps + founded
            )
            if still_present:
                item["icon"] = icon_filename
        if still_present:
            save_item_library(item)
            post_ui(schedule_icon_refresh)
        else:
            try:
                os.remove(os.path.join(ICONS_DIR, icon_filename))
            except OSError:
                pass

    threading.Thread(target=worker, daemon=True, name="icon-extractor").start()


def add_item(forced_path=None):
    if active_tab not in ("games", "apps", "founded"):
        return
    name = name_entry.get().strip()
    path = forced_path or filedialog.askopenfilename(
        title="Select Program or Script",
        filetypes=[
            ("EXE and BAT files", "*.exe *.bat"),
            ("Executable files", "*.exe"),
            ("Batch files", "*.bat"),
            ("All files", "*.*"),
        ],
    )
    path = clean_path(path)
    if not path:
        return
    if ntpath.splitext(path)[1].lower() not in (".exe", ".bat"):
        messagebox.showerror("Unsupported file", "Only .exe and .bat files can be added.")
        return
    if library_has_path(current_list(), path):
        messagebox.showwarning("Duplicate", "This executable is already in the current library.")
        return
    if not name:
        name = ntpath.splitext(ntpath.basename(path))[0]
    trainer = ""
    if active_tab == "games":
        trainer = filedialog.askopenfilename(title="Select Trainer (optional)", filetypes=[("Executable", "*.exe")]) or ""
    
    item = {
        "name": name,
        "path": path,
        "trainer": trainer,
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color()
    }
    with data_lock:
        current_list().append(item)
    save_current()
    name_entry.delete(0, tk.END)
    refresh()
    extract_icon_in_background(item, path)


def add_item_from_path(path):
    if active_tab not in ("games", "apps", "founded"):
        return
    path = clean_path(path)
    if not path or ntpath.splitext(path)[1].lower() not in (".exe", ".bat"):
        messagebox.showinfo("Unsupported file", f"Cannot add: {path}")
        return
    if library_has_path(current_list(), path):
        messagebox.showwarning("Duplicate", "This executable is already in the current library.")
        return
    item = {
        "name": ntpath.splitext(ntpath.basename(path))[0],
        "path": path,
        "trainer": "",
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color(),
    }
    with data_lock:
        current_list().append(item)
    save_current()
    refresh()
    extract_icon_in_background(item, path)


def default_drop_text():
    if DND_ACTIVE:
        return "＋  DRAG & DROP  .EXE  /  .BAT  /  .LNK   ·   ADDS TO CURRENT LIBRARY"
    return "DRAG & DROP UNAVAILABLE   ·   INSTALL tkinterdnd2"


def set_drop_status(text, foreground=SUBTEXT, background=BG2):
    global drop_reset_job
    if drop_zone is None or root is None:
        return
    try:
        drop_zone.configure(text=text)
        target_bg = BG2 if background == BG2 else lerp_color(BG2, background, 0.30)
        animate_widget_color(drop_zone, "bg", target_bg, 180, 10, CARD)
        animate_widget_color(drop_zone, "fg", foreground, 180, 10, SUBTEXT)
        animate_widget_color(
            drop_zone, "highlightbackground",
            BORDER if background == CARD else background,
            180, 10, BORDER,
        )
        if drop_reset_job is not None:
            root.after_cancel(drop_reset_job)
        drop_reset_job = root.after(3000, reset_drop_status)
    except tk.TclError:
        pass


def reset_drop_status():
    global drop_reset_job
    drop_reset_job = None
    if drop_zone is None:
        return
    try:
        drop_zone.configure(text=default_drop_text())
        animate_widget_color(drop_zone, "bg", BG2, 220, 11, BG2)
        animate_widget_color(drop_zone, "fg", SUBTEXT, 220, 11, SUBTEXT)
        animate_widget_color(drop_zone, "highlightbackground", BORDER, 220, 11, BORDER)
    except tk.TclError:
        pass


def add_dropped_paths(paths):
    """Add a group of dropped launchers with one save and one UI refresh."""
    if active_tab not in ("games", "apps", "founded"):
        return 0, 0, len(paths)
    target = current_list()
    existing = {
        canonical_path(item.get("path", ""))
        for item in target
        if item.get("path")
    }
    new_items = []
    rejected = 0
    duplicates = 0

    for raw_path in paths:
        path = clean_path(str(raw_path).strip("{}"))
        if path.lower().endswith(".lnk"):
            path = clean_path(resolve_shortcut(path))
        extension = ntpath.splitext(path)[1].lower()
        if extension not in (".exe", ".bat") or not os.path.isfile(path):
            rejected += 1
            continue
        key = canonical_path(path)
        if not key or key in existing:
            duplicates += 1
            continue
        item = {
            "name": ntpath.splitext(ntpath.basename(path))[0],
            "path": path,
            "trainer": "",
            "icon": "",
            "playtime": 0,
            "pinned": False,
            "color": random_color(),
        }
        new_items.append(item)
        existing.add(key)

    if new_items:
        with data_lock:
            target.extend(new_items)
        save_current()
        refresh(reset_page=True)
        for item in new_items:
            extract_icon_in_background(item, item["path"])

    return len(new_items), duplicates, rejected


def handle_drop_event(event):
    try:
        paths = root.tk.splitlist(event.data)
    except (tk.TclError, AttributeError):
        paths = ()
    added, duplicates, rejected = add_dropped_paths(paths)
    details = []
    if duplicates:
        details.append(f"{duplicates} duplicate")
    if rejected:
        details.append(f"{rejected} unsupported")
    suffix = f" — {', '.join(details)}" if details else ""
    if added:
        set_drop_status(f"✓ ADDED {added} ITEM{'S' if added != 1 else ''}{suffix}", TEXT, GREEN)
    else:
        set_drop_status(f"NO FILES ADDED{suffix}", TEXT, ORANGE)
    return getattr(event, "action", "copy")


def handle_drop_enter(event):
    set_drop_status("RELEASE TO ADD TO THIS LIBRARY", TEXT, ACCENT)
    return getattr(event, "action", "copy")


def handle_drop_leave(event):
    reset_drop_status()
    return getattr(event, "action", "copy")

# ==========================================
# PROCESS TRACKING
# ==========================================
def clean_path(path):
    value = os.path.expandvars(str(path or "").strip())
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


@lru_cache(maxsize=4096)
def canonical_path(path):
    value = clean_path(path)
    if not value:
        return ""
    return ntpath.normcase(ntpath.normpath(value))


WINDOWS_CRASH_CODES = {
    0xC0000005: (
        "Memory access violation",
        "The game attempted to read or write protected memory. Drivers, overlays, mods, or damaged files are common triggers.",
        "critical",
        ["Verify the game files.", "Disable overlays and third-party mods.", "Update the graphics driver."],
    ),
    0xC000001D: (
        "Illegal CPU instruction",
        "The executable used an instruction unsupported by the processor or reached damaged executable code.",
        "critical",
        ["Verify or reinstall the game.", "Remove unofficial patches.", "Check the game's CPU requirements."],
    ),
    0xC000007B: (
        "Invalid executable or runtime architecture",
        "Windows could not load a required executable image, often because 32-bit and 64-bit runtime files were mixed.",
        "critical",
        ["Repair the Visual C++ runtimes.", "Verify the game files.", "Remove manually copied DLL files."],
    ),
    0xC0000094: (
        "Integer division by zero",
        "The game encountered an unhandled arithmetic error.",
        "critical",
        ["Install the latest game patch.", "Disable mods.", "Verify the game files."],
    ),
    0xC00000FD: (
        "Stack overflow",
        "The process exhausted its call stack, commonly because of a game bug or incompatible mod.",
        "critical",
        ["Disable mods and plugins.", "Install the latest game patch.", "Reset custom configuration files."],
    ),
    0xC0000135: (
        "Required DLL was not found",
        "Windows could not locate a runtime component required during startup.",
        "critical",
        ["Verify the game files.", "Repair DirectX and Visual C++ runtimes.", "Check antivirus quarantine history."],
    ),
    0xC0000139: (
        "DLL entry point was not found",
        "A loaded DLL is incompatible with the game or with another installed runtime component.",
        "critical",
        ["Verify the game files.", "Repair Visual C++ runtimes.", "Remove unofficial DLL replacements."],
    ),
    0xC0000142: (
        "DLL initialization failed",
        "A required module was found but could not initialize.",
        "critical",
        ["Restart Windows.", "Disable overlays and injectors.", "Verify the game files."],
    ),
    0xC0000374: (
        "Heap corruption",
        "Windows detected corrupted process memory, commonly caused by a game bug, mod, overlay, or unstable hardware.",
        "critical",
        ["Disable mods and overlays.", "Update drivers.", "Run Windows Memory Diagnostic if crashes continue."],
    ),
    0xC0000409: (
        "Security fast-fail or stack buffer overrun",
        "Windows terminated the process after detecting corrupted control data or an explicit fast-fail condition.",
        "critical",
        ["Verify the game files.", "Disable mods and overlays.", "Update Windows and graphics drivers."],
    ),
}


def item_library_kind(item):
    with data_lock:
        if any(candidate is item for candidate in games):
            return "game"
        if any(candidate is item for candidate in apps):
            return "app"
    return "unknown"


def format_exit_code(exit_code):
    if exit_code is None:
        return "UNAVAILABLE"
    return f"0x{(int(exit_code) & 0xFFFFFFFF):08X}"


def analyze_process_exit(exit_code, runtime_seconds=0):
    """Translate a process result into a local, deterministic crash diagnosis."""
    if exit_code is None:
        return {
            "should_report": False,
            "cause": "Exit code unavailable",
            "details": "The process ended outside the direct launcher session.",
            "severity": "info",
            "suggestions": [],
        }
    code = int(exit_code)
    unsigned = code & 0xFFFFFFFF
    if unsigned == 0:
        return {
            "should_report": False,
            "cause": "Normal exit",
            "details": "The process returned a successful exit code.",
            "severity": "info",
            "suggestions": [],
        }
    if unsigned == 0xC000013A or code == -15:
        return {
            "should_report": False,
            "cause": "Closed or interrupted by the user",
            "details": "The process received a normal close or interruption request.",
            "severity": "info",
            "suggestions": [],
        }
    known = WINDOWS_CRASH_CODES.get(unsigned)
    if known:
        cause, details, severity, suggestions = known
        return {
            "should_report": True,
            "cause": cause,
            "details": details,
            "severity": severity,
            "suggestions": suggestions,
        }
    signal_causes = {
        -6: "Process aborted",
        -9: "Process was forcibly killed",
        -11: "Segmentation fault",
    }
    if code in signal_causes:
        return {
            "should_report": True,
            "cause": signal_causes[code],
            "details": "The operating system ended the process after a fatal signal.",
            "severity": "critical" if code in (-6, -11) else "warning",
            "suggestions": [
                "Verify the game files.",
                "Disable mods and overlays.",
                "Review the game's own log files.",
            ],
        }
    quick_failure = runtime_seconds < 12
    return {
        "should_report": True,
        "cause": "Startup failure" if quick_failure else "Application error exit",
        "details": (
            "The game returned a non-zero code shortly after launch. A missing runtime, permission issue, or damaged file is likely."
            if quick_failure else
            "The game returned a non-zero error code. Its own log files may contain additional engine-specific details."
        ),
        "severity": "warning",
        "suggestions": [
            "Verify the game files.",
            "Review the game's latest log file.",
            "Disable overlays or mods and try again.",
        ],
    }


def create_game_crash_report(pid, session, analysis, runtime_seconds):
    item = session.get("item") if isinstance(session.get("item"), dict) else {}
    ended = float(session.get("ended_at") or time.time())
    exit_code = session.get("exit_code")
    report = add_report({
        "id": uuid.uuid4().hex,
        "kind": "game_crash",
        "timestamp": record_timestamp(ended),
        "epoch": ended,
        "title": f"{item.get('name', 'Game')} stopped unexpectedly",
        "item_name": item.get("name", "Unknown game"),
        "item_path": item.get("path", ""),
        "pid": pid,
        "exit_code": exit_code,
        "exit_hex": format_exit_code(exit_code),
        "cause": analysis["cause"],
        "severity": analysis["severity"],
        "runtime_seconds": runtime_seconds,
        "details": analysis["details"],
        "source": "xvviix_exit_code_analysis",
        "suggestions": analysis["suggestions"],
    })
    add_activity(
        "crash", f"Crash detected · {item.get('name', 'Game')}", item,
        detail=f"{analysis['cause']} · {format_exit_code(exit_code)}",
        severity=analysis["severity"], epoch=ended,
    )
    return report


def finalize_process_session(pid, session):
    """Create one activity signal and, when justified, one game-crash report."""
    if session.get("launcher_shutdown") or session.get("finalized"):
        return
    session["finalized"] = True
    item = session.get("item") if isinstance(session.get("item"), dict) else {}
    ended = float(session.get("ended_at") or time.time())
    started = float(session.get("started_at") or session.get("created") or ended)
    runtime_seconds = max(0, int(ended - started))
    if session.get("end_requested") or session.get("user_ended"):
        add_activity(
            "end_task", f"Task ended · {item.get('name', 'Application')}", item,
            detail=f"PID {pid} · {format_time(runtime_seconds)} session",
            severity="warning", epoch=ended,
        )
        return
    analysis = analyze_process_exit(session.get("exit_code"), runtime_seconds)
    if session.get("library_kind") == "game" and analysis["should_report"]:
        create_game_crash_report(pid, session, analysis, runtime_seconds)
        return
    if analysis["should_report"]:
        add_activity(
            "error", f"Process error · {item.get('name', 'Application')}", item,
            detail=f"{analysis['cause']} · {format_exit_code(session.get('exit_code'))}",
            severity=analysis["severity"], epoch=ended,
        )
    else:
        add_activity(
            "closed", f"Session closed · {item.get('name', 'Application')}", item,
            detail=f"{format_time(runtime_seconds)} session",
            severity="info", epoch=ended,
        )


def register_process(item, process, executable_path):
    now = time.time()
    library_kind = item_library_kind(item)
    with process_lock:
        tracked_processes[process.pid] = {
            "item": item,
            "path": canonical_path(executable_path),
            "created": now,
            "started_at": now,
            "last_accounted": now,
            "ended_at": None,
            "exit_code": None,
            "library_kind": library_kind,
            "end_requested": False,
            "user_ended": False,
            "process": process,
        }
    add_activity(
        "launch", f"Launched · {item.get('name', 'Application')}", item,
        detail=f"PID {process.pid} · {library_kind.upper()}", severity="success", epoch=now,
    )
    if root is not None:
        post_ui(refresh)
    threading.Thread(
        target=wait_for_direct_process,
        args=(process.pid, process),
        daemon=True,
        name=f"process-wait-{process.pid}",
    ).start()


def wait_for_direct_process(pid, process):
    exit_code = None
    try:
        exit_code = process.wait()
    except OSError:
        pass
    ended_at = time.time()
    with process_lock:
        session = tracked_processes.get(pid)
        if session is not None and session.get("process") is process:
            session["ended_at"] = ended_at
            session["exit_code"] = exit_code
    account_tracked_processes()


def _process_is_alive(pid, session):
    if session.get("ended_at") is not None:
        return False
    direct_process = session.get("process")
    if direct_process is not None:
        return direct_process.poll() is None
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - session["created"]) > 0.01:
            return False
        executable = process.exe()
        return not session["path"] or canonical_path(executable) == session["path"]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


def account_tracked_processes(remove_all=False, persist=True):
    """Checkpoint playtime, finalize stopped sessions, and batch disk/UI updates."""
    now = time.time()
    changed = False
    finished_sessions = []
    with process_lock, data_lock:
        game_ids = {id(item) for item in games}
        app_ids = {id(item) for item in apps}
        for pid, session in list(tracked_processes.items()):
            alive = not remove_all and _process_is_alive(pid, session)
            cutoff = min(now, session.get("ended_at") or now)
            elapsed = max(0, int(cutoff - session["last_accounted"]))
            if elapsed:
                item = session["item"]
                item["playtime"] = max(0, int(item.get("playtime", 0))) + elapsed
                session["last_accounted"] += elapsed
                if id(item) in game_ids:
                    dirty_playtime_libraries.add("games")
                elif id(item) in app_ids:
                    dirty_playtime_libraries.add("apps")
                changed = True
            if not alive:
                tracked_processes.pop(pid, None)
                if session.get("ended_at") is None:
                    session["ended_at"] = now
                if remove_all:
                    session["launcher_shutdown"] = True
                finished_sessions.append((pid, session))

    persist_now = persist or bool(finished_sessions) or remove_all
    persisted = False
    if persist_now:
        if "games" in dirty_playtime_libraries and _save(GAMES_FILE, games):
            dirty_playtime_libraries.discard("games")
            persisted = True
        if "apps" in dirty_playtime_libraries and _save(APPS_FILE, apps):
            dirty_playtime_libraries.discard("apps")
            persisted = True
    for pid, session in finished_sessions:
        finalize_process_session(pid, session)
    if (changed and persist_now) or persisted or finished_sessions:
        post_ui(refresh)
    return changed, persisted


def monitor_running_apps():
    if not HAS_PSUTIL:
        return
    last_persist = time.monotonic()
    while not monitor_stop.wait(5):
        path_index = {}
        with data_lock:
            for library_kind, library in (("game", games), ("app", apps)):
                for item in library:
                    normalized = canonical_path(item.get("path", ""))
                    if normalized:
                        path_index.setdefault(normalized, (item, library_kind))
        detected_sessions = []
        try:
            for process in psutil.process_iter(["pid", "exe", "create_time"]):
                try:
                    executable = process.info.get("exe")
                    match = path_index.get(canonical_path(executable)) if executable else None
                    if match is None:
                        continue
                    item, library_kind = match
                    with process_lock:
                        if process.pid not in tracked_processes:
                            created = float(process.info.get("create_time") or time.time())
                            tracked_processes[process.pid] = {
                                "item": item,
                                "path": canonical_path(executable),
                                "created": created,
                                "started_at": created,
                                "last_accounted": max(created, time.time() - 5),
                                "ended_at": None,
                                "exit_code": None,
                                "library_kind": library_kind,
                                "end_requested": False,
                                "user_ended": False,
                                "process": None,
                            }
                            detected_sessions.append((process.pid, item, library_kind, created))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    continue
            for pid, item, library_kind, _created in detected_sessions:
                add_activity(
                    "detected", f"Running · {item.get('name', 'Application')}", item,
                    detail=f"PID {pid} · {library_kind.upper()} detected",
                    severity="success", epoch=time.time(),
                )
            if detected_sessions:
                post_ui(refresh)
            persist_now = time.monotonic() - last_persist >= 30
            account_tracked_processes(persist=persist_now)
            if persist_now:
                last_persist = time.monotonic()
        except (psutil.Error, OSError) as exc:
            LOG.warning("Process monitor cycle failed: %s", exc)


def running_sessions_for_item(item):
    with process_lock:
        return [
            (pid, session) for pid, session in tracked_processes.items()
            if session.get("item") is item and session.get("ended_at") is None
        ]


def is_item_running(item):
    return bool(running_sessions_for_item(item))


def _terminate_tracked_session(pid, session):
    """Terminate one validated process tree without ever using a name-only match."""
    if HAS_PSUTIL:
        try:
            process = psutil.Process(pid)
            created = session.get("created")
            if session.get("process") is None and created is not None:
                if abs(process.create_time() - float(created)) > 0.05:
                    raise RuntimeError("PID was reused by a different process")
            children = process.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                return
            targets = children + [process]
            _, alive = psutil.wait_procs(targets, timeout=2.0)
            for remaining in alive:
                try:
                    remaining.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                _, alive = psutil.wait_procs(alive, timeout=1.5)
            if alive:
                raise RuntimeError("one or more processes did not stop")
            return
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, psutil.Error, OSError) as exc:
            raise RuntimeError(str(exc)) from exc

    direct_process = session.get("process")
    if direct_process is None:
        raise RuntimeError("process control requires psutil for externally started programs")
    try:
        direct_process.terminate()
        deadline = time.monotonic() + 2.0
        while direct_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if direct_process.poll() is None:
            direct_process.kill()
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc


def finish_end_task_ui(item_name, ended_count, failures):
    refresh()
    if failures:
        detail = "\n".join(f"PID {pid}: {reason}" for pid, reason in failures[:5])
        if ended_count:
            messagebox.showwarning(
                "End Task partially completed",
                f"Stopped {ended_count} process(es) for {item_name}.\n\n{detail}",
            )
        else:
            messagebox.showerror(
                "End Task failed",
                f"XVVIIX could not stop {item_name}.\n\n{detail}\n\nTry running XVVIIX as administrator.",
            )


def end_task(item):
    sessions = running_sessions_for_item(item)
    if not sessions:
        messagebox.showinfo("End Task", f"{item.get('name', 'This item')} is not currently running.")
        refresh()
        return
    if not messagebox.askyesno(
        "End Task",
        f"End {item.get('name', 'this task')} and its child processes?\n\nUnsaved progress may be lost.",
        icon="warning",
    ):
        return
    with process_lock:
        for _pid, session in sessions:
            session["end_requested"] = True
    refresh()

    def worker():
        ended_count = 0
        failures = []
        for pid, session in sessions:
            try:
                _terminate_tracked_session(pid, session)
                with process_lock:
                    session["user_ended"] = True
                    if session.get("ended_at") is None:
                        session["ended_at"] = time.time()
                ended_count += 1
            except RuntimeError as exc:
                with process_lock:
                    session["end_requested"] = False
                failures.append((pid, str(exc)))
        account_tracked_processes()
        post_ui(finish_end_task_ui, item.get("name", "Application"), ended_count, failures)

    threading.Thread(
        target=worker, daemon=True,
        name=f"end-task-{canonical_path(item.get('path', ''))[-24:] or 'process'}",
    ).start()


def run_as_admin(path, cwd=None):
    if os.name != "nt":
        messagebox.showerror("Admin Error", "Administrator launching is available only on Windows.")
        return False
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, None, cwd, 1)
        if result <= 32:
            raise OSError(f"ShellExecuteW failed with code {result}")
        return True
    except (AttributeError, OSError) as exc:
        messagebox.showerror("Admin Error", str(exc))
        return False


def run_only(item):
    path = clean_path(item.get("path", ""))
    extension = ntpath.splitext(path)[1].lower()
    if extension not in (".bat", ".exe"):
        messagebox.showerror("Error", "Only .exe and .bat files are supported")
        return
    if not os.path.isfile(path):
        messagebox.showerror("Error", f"File not found:\n{path}")
        return
    game_folder = os.path.dirname(path) or None
    try:
        if extension == ".bat":
            command_processor = os.environ.get("COMSPEC", "cmd.exe")
            command = [command_processor, "/d", "/c", path]
        else:
            command = [path]
        process = subprocess.Popen(command, cwd=game_folder, shell=False)
        register_process(item, process, path)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            run_as_admin(path, game_folder)
        else:
            messagebox.showerror("Launch Error", f"Could not start:\n{exc}")


def run_with_trainer(item):
    trainer = clean_path(item.get("trainer", ""))
    if trainer:
        if not os.path.isfile(trainer):
            messagebox.showerror("Trainer Error", f"Trainer not found:\n{trainer}")
            return
        try:
            subprocess.Popen([trainer], cwd=os.path.dirname(trainer) or None, shell=False)
        except OSError as exc:
            messagebox.showerror("Trainer Error", f"Could not start trainer:\n{exc}")
            return
    run_only(item)

# ==========================================
# SYSTEM REPORT
# ==========================================
class SystemReportCancelled(Exception):
    pass


def format_bytes(value):
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    precision = 0 if index == 0 else (1 if amount < 100 else 0)
    return f"{amount:.{precision}f} {units[index]}"


def _system_report_progress(callback, percent, phase, detail=""):
    if sys_report_cancel.is_set():
        raise SystemReportCancelled()
    if callback:
        callback(max(0, min(100, int(percent))), phase, detail)


def _powershell_system_inventory():
    if os.name != "nt":
        return {}
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        return {}
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem
$os=Get-CimInstance Win32_OperatingSystem
$bios=Get-CimInstance Win32_BIOS
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu=@(Get-CimInstance Win32_VideoController | ForEach-Object {
  [pscustomobject]@{Name=$_.Name;DriverVersion=$_.DriverVersion;AdapterRAM=$_.AdapterRAM;Status=$_.Status}
})
$def=Get-MpComputerStatus
$fw=@(Get-NetFirewallProfile | ForEach-Object {[pscustomobject]@{Name=$_.Name;Enabled=$_.Enabled}})
$bad=@(Get-CimInstance Win32_PnPEntity | Where-Object {$_.ConfigManagerErrorCode -ne 0} | Select-Object -First 20 Name,ConfigManagerErrorCode)
$tpm=Get-Tpm
[pscustomobject]@{
 Computer=[pscustomobject]@{Manufacturer=$cs.Manufacturer;Model=$cs.Model;TotalPhysicalMemory=$cs.TotalPhysicalMemory}
 OS=[pscustomobject]@{Caption=$os.Caption;Version=$os.Version;BuildNumber=$os.BuildNumber;InstallDate=$os.InstallDate}
 BIOS=[pscustomobject]@{Manufacturer=$bios.Manufacturer;Version=($bios.SMBIOSBIOSVersion -join ', ');ReleaseDate=$bios.ReleaseDate}
 CPU=[pscustomobject]@{Name=$cpu.Name;MaxClockSpeed=$cpu.MaxClockSpeed;Cores=$cpu.NumberOfCores;Logical=$cpu.NumberOfLogicalProcessors}
 GPU=$gpu
 Defender=[pscustomobject]@{AntivirusEnabled=$def.AntivirusEnabled;RealTimeProtectionEnabled=$def.RealTimeProtectionEnabled;SignaturesOutOfDate=$def.AntivirusSignatureOutOfDate}
 Firewall=$fw
 DeviceErrors=$bad
 TPM=[pscustomobject]@{Present=$tpm.TpmPresent;Ready=$tpm.TpmReady;Enabled=$tpm.TpmEnabled}
} | ConvertTo-Json -Depth 6 -Compress
"""
    process = None
    try:
        process = subprocess.Popen(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 15
        while True:
            if sys_report_cancel.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise SystemReportCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                return {}
            try:
                stdout, _stderr = process.communicate(timeout=min(0.15, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        output = stdout.strip()
        if not output or len(output) > 2 * 1024 * 1024:
            return {}
        return json.loads(output)
    except SystemReportCancelled:
        raise
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeError) as exc:
        LOG.debug("Windows system inventory unavailable: %s", exc)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        return {}


def _cpu_model_name():
    name = platform.processor().strip()
    if name:
        return name
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.casefold().startswith("model name") and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.machine() or "Unknown processor"


def _as_record_list(value):
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def collect_system_report(progress_callback=None):
    """Collect a bounded local hardware, OS, storage, network, and security report."""
    started = time.perf_counter()
    epoch = time.time()
    _system_report_progress(progress_callback, 3, "INITIALIZING", "Building local diagnostic inventory")
    hostname = socket.gethostname() or platform.node() or "Unknown device"
    os_name = platform.system() or "Unknown OS"
    os_release = platform.release()
    os_version = platform.version()
    architecture = platform.machine() or "Unknown"
    python_version = platform.python_version()
    sections = []
    findings = []
    score = 100

    def finding(severity, title, detail, action="", penalty=0):
        nonlocal score
        findings.append({
            "severity": severity, "title": title,
            "detail": detail, "action": action,
        })
        score = max(0, score - max(0, int(penalty)))

    _system_report_progress(progress_callback, 12, "PLATFORM", "Reading operating system and firmware identity")
    windows = _powershell_system_inventory()
    computer = windows.get("Computer") if isinstance(windows.get("Computer"), dict) else {}
    windows_os = windows.get("OS") if isinstance(windows.get("OS"), dict) else {}
    bios = windows.get("BIOS") if isinstance(windows.get("BIOS"), dict) else {}
    manufacturer = str(computer.get("Manufacturer") or "Unknown")
    model = str(computer.get("Model") or platform.node() or "Unknown")
    os_label = str(windows_os.get("Caption") or f"{os_name} {os_release}").strip()
    build = str(windows_os.get("BuildNumber") or os_version or "Unknown")
    boot_epoch = 0.0
    uptime_seconds = 0
    if HAS_PSUTIL:
        try:
            boot_epoch = float(psutil.boot_time())
            uptime_seconds = max(0, int(time.time() - boot_epoch))
        except (psutil.Error, OSError, ValueError):
            pass
    platform_items = [
        {"label": "HOSTNAME", "value": hostname, "status": "success"},
        {"label": "DEVICE", "value": f"{manufacturer} {model}".strip(), "status": "info"},
        {"label": "OPERATING SYSTEM", "value": os_label, "status": "success"},
        {"label": "BUILD / KERNEL", "value": build, "status": "info"},
        {"label": "ARCHITECTURE", "value": architecture, "status": "info"},
        {"label": "UPTIME", "value": format_time(uptime_seconds), "status": "warning" if uptime_seconds > 14 * 86400 else "success"},
    ]
    if bios:
        platform_items.append({
            "label": "BIOS", "value": f"{bios.get('Manufacturer') or ''} {bios.get('Version') or ''}".strip() or "Unknown",
            "status": "info",
        })
    sections.append({"title": "01  COMMAND PLATFORM", "status": "success", "items": platform_items})
    if uptime_seconds > 14 * 86400:
        finding(
            "warning", "Extended system uptime",
            f"The computer has been running for {format_time(uptime_seconds)}.",
            "Restart Windows before diagnosing intermittent game or driver problems.", 4,
        )

    _system_report_progress(progress_callback, 27, "COMPUTE CORE", "Sampling processor topology and load")
    cpu_model = _cpu_model_name()
    cpu_physical = os.cpu_count() or 0
    cpu_logical = os.cpu_count() or 0
    cpu_percent = 0.0
    frequency = 0.0
    process_count = 0
    if HAS_PSUTIL:
        try:
            cpu_physical = psutil.cpu_count(logical=False) or cpu_physical
            cpu_logical = psutil.cpu_count(logical=True) or cpu_logical
            cpu_percent = float(psutil.cpu_percent(interval=0.25))
            freq = psutil.cpu_freq()
            frequency = float(freq.max or freq.current or 0.0) if freq else 0.0
            process_count = len(psutil.pids())
        except (psutil.Error, OSError, ValueError):
            pass
    win_cpu = windows.get("CPU") if isinstance(windows.get("CPU"), dict) else {}
    cpu_model = str(win_cpu.get("Name") or cpu_model).strip()
    frequency = float(win_cpu.get("MaxClockSpeed") or frequency or 0.0)
    cpu_physical = int(win_cpu.get("Cores") or cpu_physical or 0)
    cpu_logical = int(win_cpu.get("Logical") or cpu_logical or 0)
    cpu_status = "critical" if cpu_percent >= 95 else ("warning" if cpu_percent >= 85 else "success")
    sections.append({
        "title": "02  COMPUTE CORE", "status": cpu_status,
        "items": [
            {"label": "PROCESSOR", "value": cpu_model, "status": "info"},
            {"label": "TOPOLOGY", "value": f"{cpu_physical} physical / {cpu_logical} logical cores", "status": "success"},
            {"label": "MAX FREQUENCY", "value": f"{frequency / 1000:.2f} GHz" if frequency else "Unavailable", "status": "info"},
            {"label": "UTILIZATION SAMPLE", "value": f"{cpu_percent:.1f}%", "status": cpu_status},
            {"label": "ACTIVE PROCESSES", "value": str(process_count) if process_count else "Unavailable", "status": "info"},
        ],
    })
    if cpu_percent >= 95:
        finding("critical", "Processor saturation", f"CPU utilization sampled at {cpu_percent:.1f}%.", "Close CPU-heavy background tasks and scan for runaway processes.", 12)
    elif cpu_percent >= 85:
        finding("warning", "High processor load", f"CPU utilization sampled at {cpu_percent:.1f}%.", "Repeat the report after closing background workloads.", 6)

    _system_report_progress(progress_callback, 42, "MEMORY", "Measuring physical and virtual memory pressure")
    memory_total = memory_available = memory_used = 0
    memory_percent = swap_percent = 0.0
    if HAS_PSUTIL:
        try:
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            memory_total = int(memory.total)
            memory_available = int(memory.available)
            memory_used = int(memory.used)
            memory_percent = float(memory.percent)
            swap_percent = float(swap.percent)
        except (psutil.Error, OSError, ValueError):
            pass
    if not memory_total:
        try:
            memory_total = int(computer.get("TotalPhysicalMemory") or 0)
        except (TypeError, ValueError):
            memory_total = 0
    memory_status = "critical" if memory_percent >= 95 else ("warning" if memory_percent >= 85 else "success")
    sections.append({
        "title": "03  MEMORY ARRAY", "status": memory_status,
        "items": [
            {"label": "INSTALLED", "value": format_bytes(memory_total), "status": "info"},
            {"label": "IN USE", "value": format_bytes(memory_used), "status": memory_status},
            {"label": "AVAILABLE", "value": format_bytes(memory_available), "status": memory_status},
            {"label": "MEMORY LOAD", "value": f"{memory_percent:.1f}%", "status": memory_status},
            {"label": "SWAP LOAD", "value": f"{swap_percent:.1f}%", "status": "warning" if swap_percent >= 75 else "info"},
        ],
    })
    if memory_percent >= 95:
        finding("critical", "Critical memory pressure", f"Physical memory usage is {memory_percent:.1f}%.", "Close memory-heavy applications or increase available RAM.", 15)
    elif memory_percent >= 85:
        finding("warning", "High memory pressure", f"Physical memory usage is {memory_percent:.1f}%.", "Close unused applications before launching a game.", 7)

    _system_report_progress(progress_callback, 56, "GRAPHICS", "Inspecting graphics adapters and drivers")
    gpu_records = _as_record_list(windows.get("GPU"))
    graphics_items = []
    gpu_names = []
    for index, gpu in enumerate(gpu_records[:6], start=1):
        name = str(gpu.get("Name") or f"Graphics adapter {index}")
        gpu_names.append(name)
        try:
            vram = int(gpu.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            vram = 0
        value = name
        if vram:
            value += f" · {format_bytes(vram)}"
        if gpu.get("DriverVersion"):
            value += f" · DRIVER {gpu.get('DriverVersion')}"
        graphics_items.append({"label": f"ADAPTER {index}", "value": value, "status": "success"})
    if not graphics_items:
        graphics_items.append({"label": "ADAPTER", "value": "Detailed GPU inventory unavailable", "status": "info"})
    sections.append({"title": "04  GRAPHICS SUBSYSTEM", "status": "success" if gpu_names else "info", "items": graphics_items})

    _system_report_progress(progress_callback, 68, "STORAGE", "Mapping local volumes and capacity margins")
    storage_items = []
    storage_total = storage_free = 0
    if HAS_PSUTIL:
        seen_mounts = set()
        try:
            for partition in psutil.disk_partitions(all=False)[:20]:
                mount = partition.mountpoint
                if mount in seen_mounts:
                    continue
                seen_mounts.add(mount)
                try:
                    usage = psutil.disk_usage(mount)
                except (PermissionError, psutil.Error, OSError):
                    continue
                storage_total += int(usage.total)
                storage_free += int(usage.free)
                free_percent = 100.0 - float(usage.percent)
                status = "critical" if free_percent < 5 else ("warning" if free_percent < 12 else "success")
                storage_items.append({
                    "label": mount,
                    "value": f"{format_bytes(usage.free)} free / {format_bytes(usage.total)} · {usage.percent:.1f}% used",
                    "status": status,
                })
                if free_percent < 5:
                    finding("critical", f"Critical storage margin on {mount}", f"Only {free_percent:.1f}% remains free.", "Free disk space before installing or updating games.", 15)
                elif free_percent < 12:
                    finding("warning", f"Low storage margin on {mount}", f"Only {free_percent:.1f}% remains free.", "Remove temporary files or move unused games.", 7)
        except (psutil.Error, OSError):
            pass
    if not storage_items:
        storage_items.append({"label": "VOLUMES", "value": "Storage inventory unavailable", "status": "warning"})
    storage_status = "critical" if any(item["status"] == "critical" for item in storage_items) else ("warning" if any(item["status"] == "warning" for item in storage_items) else "success")
    sections.append({"title": "05  STORAGE MODULES", "status": storage_status, "items": storage_items})

    _system_report_progress(progress_callback, 78, "NETWORK", "Enumerating active local network links")
    network_items = []
    active_interfaces = []
    ipv4_addresses = []
    if HAS_PSUTIL:
        try:
            stats = psutil.net_if_stats()
            addresses = psutil.net_if_addrs()
            for name, stat in list(stats.items())[:24]:
                if not stat.isup:
                    continue
                active_interfaces.append(name)
                speed = f"{stat.speed} Mbps" if stat.speed and stat.speed > 0 else "speed unavailable"
                ips = [
                    address.address for address in addresses.get(name, [])
                    if address.family == socket.AF_INET and not address.address.startswith("127.")
                ]
                ipv4_addresses.extend(ips)
                value = speed + (f" · {', '.join(ips[:3])}" if ips else "")
                network_items.append({"label": name, "value": value, "status": "success"})
        except (psutil.Error, OSError, ValueError):
            pass
    if not network_items:
        network_items.append({"label": "LINK STATUS", "value": "No active interface details available", "status": "warning"})
    sections.append({"title": "06  NETWORK LINKS", "status": "success" if active_interfaces else "warning", "items": network_items})

    _system_report_progress(progress_callback, 87, "POWER / SECURITY", "Checking battery, thermal, firewall, and protection state")
    power_items = []
    battery_percent = None
    battery_plugged = None
    temperatures = []
    if HAS_PSUTIL:
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_percent = float(battery.percent)
                battery_plugged = bool(battery.power_plugged)
                power_items.append({
                    "label": "BATTERY", "value": f"{battery_percent:.0f}% · {'AC POWER' if battery_plugged else 'ON BATTERY'}",
                    "status": "warning" if battery_percent < 20 and not battery_plugged else "success",
                })
        except (psutil.Error, OSError, ValueError, AttributeError):
            pass
        try:
            sensor_map = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
            for group, entries in list(sensor_map.items())[:8]:
                for entry in entries[:4]:
                    if entry.current is not None:
                        temperatures.append((entry.label or group, float(entry.current)))
        except (psutil.Error, OSError, ValueError, AttributeError):
            pass
    for label, temperature in temperatures[:8]:
        status = "critical" if temperature >= 90 else ("warning" if temperature >= 80 else "success")
        power_items.append({"label": label.upper(), "value": f"{temperature:.1f} °C", "status": status})
        if temperature >= 90:
            finding("critical", "Critical thermal reading", f"{label} reported {temperature:.1f} °C.", "Stop heavy workloads and inspect cooling immediately.", 15)
        elif temperature >= 80:
            finding("warning", "Elevated thermal reading", f"{label} reported {temperature:.1f} °C.", "Inspect airflow and cooling before long gaming sessions.", 7)
    if battery_percent is not None and battery_percent < 20 and not battery_plugged:
        finding("warning", "Low battery reserve", f"Battery is at {battery_percent:.0f}%.", "Connect AC power before launching a demanding game.", 4)
    if not power_items:
        power_items.append({"label": "POWER / THERMAL", "value": "No battery or thermal sensors exposed", "status": "info"})

    security_items = []
    defender = windows.get("Defender") if isinstance(windows.get("Defender"), dict) else {}
    defender_available = defender and any(
        defender.get(field) is not None
        for field in ("AntivirusEnabled", "RealTimeProtectionEnabled", "SignaturesOutOfDate")
    )
    if defender_available:
        antivirus = bool(defender.get("AntivirusEnabled"))
        realtime = bool(defender.get("RealTimeProtectionEnabled"))
        signatures_old = bool(defender.get("SignaturesOutOfDate"))
        security_items.extend([
            {"label": "MICROSOFT DEFENDER", "value": "ENABLED" if antivirus else "DISABLED", "status": "success" if antivirus else "critical"},
            {"label": "REAL-TIME PROTECTION", "value": "ENABLED" if realtime else "DISABLED", "status": "success" if realtime else "critical"},
            {"label": "SIGNATURE STATUS", "value": "OUT OF DATE" if signatures_old else "CURRENT", "status": "warning" if signatures_old else "success"},
        ])
        if not antivirus or not realtime:
            finding("critical", "Real-time malware protection disabled", "Microsoft Defender reported inactive protection.", "Enable an antivirus provider and real-time protection.", 15)
        elif signatures_old:
            finding("warning", "Antivirus signatures out of date", "Defender signatures require an update.", "Run Windows Update or update Defender signatures.", 5)
    firewall_records = _as_record_list(windows.get("Firewall"))
    disabled_firewalls = []
    for firewall in firewall_records:
        enabled = bool(firewall.get("Enabled"))
        name = str(firewall.get("Name") or "Profile")
        security_items.append({"label": f"FIREWALL {name.upper()}", "value": "ENABLED" if enabled else "DISABLED", "status": "success" if enabled else "warning"})
        if not enabled:
            disabled_firewalls.append(name)
    if disabled_firewalls:
        finding("warning", "Firewall profile disabled", ", ".join(disabled_firewalls) + " firewall profile(s) are disabled.", "Enable the profile unless another managed firewall replaces it.", 5)
    tpm = windows.get("TPM") if isinstance(windows.get("TPM"), dict) else {}
    if tpm:
        security_items.append({"label": "TPM", "value": "READY" if tpm.get("Ready") else ("PRESENT" if tpm.get("Present") else "NOT PRESENT"), "status": "success" if tpm.get("Ready") else "info"})
    device_errors = _as_record_list(windows.get("DeviceErrors"))
    security_items.append({"label": "DEVICE ERRORS", "value": str(len(device_errors)), "status": "warning" if device_errors else "success"})
    if device_errors:
        names = ", ".join(str(entry.get("Name") or "Unknown device") for entry in device_errors[:5])
        finding("warning", "Windows device errors detected", names, "Open Device Manager and inspect devices showing warning symbols.", 10)
    if not security_items:
        security_items.append({"label": "SECURITY TELEMETRY", "value": "Detailed Windows security telemetry unavailable", "status": "info"})
    sections.append({"title": "07  POWER / THERMAL", "status": "warning" if any(item["status"] in ("warning", "critical") for item in power_items) else "success", "items": power_items})
    sections.append({"title": "08  SECURITY PERIMETER", "status": "warning" if any(item["status"] in ("warning", "critical") for item in security_items) else "success", "items": security_items})

    _system_report_progress(progress_callback, 94, "LAUNCH ENVIRONMENT", "Recording launcher runtime and local capabilities")
    environment_items = [
        {"label": "PYTHON", "value": python_version, "status": "success"},
        {"label": "XVVIIX DATA", "value": BASE_DIR, "status": "info"},
        {"label": "PROCESS MONITOR", "value": "ONLINE" if HAS_PSUTIL else "UNAVAILABLE", "status": "success" if HAS_PSUTIL else "warning"},
        {"label": "DATA VAULT", "value": "UNLOCKED / AES-256-GCM", "status": "success" if vault_key is not None else "warning"},
        {"label": "ADMINISTRATOR", "value": "YES" if is_admin() else "NO", "status": "info"},
    ]
    sections.append({"title": "09  XVVIIX ENVIRONMENT", "status": "success", "items": environment_items})

    if not findings:
        findings.append({
            "severity": "success", "title": "All monitored systems nominal",
            "detail": "No critical condition was detected in the available local telemetry.",
            "action": "Retain this report as a healthy baseline.",
        })
    score = max(0, min(100, score))
    severity = "critical" if score < 60 else ("warning" if score < 85 else "success")
    duration_ms = int((time.perf_counter() - started) * 1000)
    summary = {
        "device": f"{manufacturer} {model}".strip(),
        "os": os_label,
        "cpu": cpu_model,
        "gpu": ", ".join(gpu_names[:3]) or "Detailed GPU inventory unavailable",
        "memory": format_bytes(memory_total),
        "storage": f"{format_bytes(storage_free)} free / {format_bytes(storage_total)}",
        "network": f"{len(active_interfaces)} active interface(s)",
        "uptime": format_time(uptime_seconds),
    }
    _system_report_progress(progress_callback, 100, "COMPLETE", f"Health score {score}/100")
    return {
        "id": uuid.uuid4().hex,
        "kind": "system_report",
        "timestamp": record_timestamp(epoch),
        "epoch": epoch,
        "title": f"SYS REPORT · {hostname}",
        "item_name": hostname,
        "item_path": "",
        "pid": 0,
        "exit_code": None,
        "exit_hex": "",
        "cause": f"SYSTEM HEALTH {score}/100",
        "severity": severity,
        "runtime_seconds": 0,
        "details": f"Comprehensive local diagnostic completed in {duration_ms / 1000:.1f} seconds.",
        "source": "xvviix_sys_report_v1",
        "health_score": score,
        "scan_duration_ms": duration_ms,
        "summary": summary,
        "sections": sections,
        "findings": findings,
        "suggestions": [entry.get("action", "") for entry in findings if entry.get("action")][:8],
    }


# ==========================================
# ANIMATIONS
# ==========================================
def ease_out_cubic(value):
    return 1 - (1 - value) ** 3


def ease_in_out_cubic(value):
    if value < 0.5:
        return 4 * value ** 3
    return 1 - ((-2 * value + 2) ** 3) / 2


def widget_exists(widget):
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def cancel_animation(widget, channel):
    key = (id(widget), channel)
    animation_id = _anims.pop(key, None)
    _anim_tokens.pop(key, None)
    if animation_id is not None and root is not None:
        try:
            root.after_cancel(animation_id)
        except (tk.TclError, ValueError):
            pass


def start_animation(widget, channel, duration, steps, updater, easing=ease_out_cubic):
    """Run one cancellable animation per widget/channel."""
    global _animation_serial
    if root is None or not widget_exists(widget):
        return
    key = (id(widget), channel)
    cancel_animation(widget, channel)
    _animation_serial += 1
    token = _animation_serial
    _anim_tokens[key] = token
    frame_delay = max(8, int(duration / max(1, steps)))

    def tick(step=1):
        if _anim_tokens.get(key) != token or not widget_exists(widget):
            return
        progress = min(1.0, step / max(1, steps))
        try:
            updater(easing(progress))
        except (tk.TclError, ValueError, TypeError):
            _anims.pop(key, None)
            _anim_tokens.pop(key, None)
            return
        if step < steps:
            _anims[key] = root.after(frame_delay, tick, step + 1)
        else:
            _anims.pop(key, None)
            _anim_tokens.pop(key, None)

    _anims[key] = root.after(0, tick)


def resolve_widget_color(widget, color, fallback="#ffffff"):
    value = str(color)
    try:
        hex_to_rgb(value)
        return value
    except (TypeError, ValueError):
        try:
            red, green, blue = widget.winfo_rgb(value)
            return rgb_to_hex(red // 256, green // 256, blue // 256)
        except (tk.TclError, TypeError, ValueError):
            return fallback


def widget_color(widget, prop, fallback):
    try:
        return resolve_widget_color(widget, widget.cget(prop), fallback)
    except tk.TclError:
        return resolve_widget_color(widget, fallback)


def animate_widget_color(widget, prop, target, duration=150, steps=9, fallback=None):
    if not widget_exists(widget):
        return
    target = resolve_widget_color(widget, target, fallback or "#ffffff")
    start = widget_color(widget, prop, resolve_widget_color(widget, fallback or target))
    if start.lower() == target.lower():
        try:
            widget.configure(**{prop: target})
        except tk.TclError:
            pass
        return

    def update(progress):
        widget.configure(**{prop: lerp_color(start, target, progress)})

    start_animation(widget, f"color:{prop}", duration, steps, update)


def animate_numeric(widget, channel, start, target, setter, duration=150, steps=9):
    difference = target - start
    start_animation(
        widget,
        channel,
        duration,
        steps,
        lambda progress: setter(start + difference * progress),
        easing=ease_in_out_cubic,
    )


def current_grid_margin(widget, fallback):
    try:
        value = widget.grid_info().get("padx", fallback)
        if isinstance(value, (tuple, list)):
            value = value[0]
        return int(float(value))
    except (tk.TclError, TypeError, ValueError):
        return fallback


def animate_grid_margin(widget, target, duration=150):
    start = current_grid_margin(widget, target)
    animate_numeric(
        widget,
        "grid-margin",
        start,
        target,
        lambda value: widget.grid_configure(padx=int(round(value)), pady=int(round(value))),
        duration=duration,
        steps=8,
    )


def pointer_is_inside(widget):
    if not widget_exists(widget):
        return False
    try:
        pointed = root.winfo_containing(root.winfo_pointerx(), root.winfo_pointery())
        while pointed is not None:
            if pointed == widget:
                return True
            pointed = pointed.master
    except (tk.TclError, AttributeError):
        return False
    return False


def bind_animated_button(button, idle_bg, hover_bg, idle_fg=None, hover_fg=None, sound=False):
    """Attach interruption-safe hover, press, and release transitions."""
    button._anim_idle_bg = idle_bg
    button._anim_hover_bg = hover_bg
    button._anim_idle_fg = idle_fg
    button._anim_hover_fg = hover_fg

    def enter(_event=None):
        if str(button.cget("state")) == "disabled":
            return
        animate_widget_color(button, "bg", button._anim_hover_bg, 130, 8, idle_bg)
        if button._anim_hover_fg:
            animate_widget_color(button, "fg", button._anim_hover_fg, 130, 8, idle_fg)
        if sound:
            play_sound("hover")

    def leave(_event=None):
        animate_widget_color(button, "bg", button._anim_idle_bg, 180, 10, hover_bg)
        if button._anim_idle_fg:
            animate_widget_color(button, "fg", button._anim_idle_fg, 180, 10, hover_fg)

    def press(_event=None):
        if str(button.cget("state")) == "disabled":
            return
        pressed = lerp_color(button._anim_hover_bg, "#000000", 0.20)
        animate_widget_color(button, "bg", pressed, 70, 5, button._anim_hover_bg)
        play_sound("click")

    def release(_event=None):
        target = button._anim_hover_bg if pointer_is_inside(button) else button._anim_idle_bg
        animate_widget_color(button, "bg", target, 110, 7, button._anim_idle_bg)

    button.bind("<Enter>", enter)
    button.bind("<Leave>", leave)
    button.bind("<ButtonPress-1>", press, add="+")
    button.bind("<ButtonRelease-1>", release, add="+")
    return button


def animate_tab_state(button, selected):
    button._anim_idle_bg = TAB_ACT if selected else TAB_IN
    button._anim_hover_bg = ACCENT2 if selected else CARD2
    button._anim_idle_fg = TEXT if selected else SUBTEXT
    button._anim_hover_fg = TEXT
    animate_widget_color(button, "bg", button._anim_idle_bg, 180, 10, TAB_IN)
    animate_widget_color(button, "fg", button._anim_idle_fg, 180, 10, SUBTEXT)


def animate_entry_focus(entry, focused):
    animate_widget_color(
        entry,
        "highlightbackground",
        NEON if focused else BORDER,
        duration=160,
        steps=9,
        fallback=BORDER,
    )
    animate_widget_color(
        entry,
        "bg",
        lerp_color(CARD2, ACCENT, 0.10) if focused else CARD2,
        duration=160,
        steps=9,
        fallback=CARD2,
    )


def animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, hovered):
    if not widget_exists(card):
        return
    card._card_hovered = hovered
    surface_target = lerp_color(CARD, accent_color, 0.10) if hovered else CARD
    border_target = accent_color if hovered else BORDER
    stripe_target = lerp_color(accent_color, "#ffffff", 0.30) if hovered else accent_color
    card.configure(highlightthickness=2 if hovered else 1)
    animate_widget_color(card, "bg", surface_target, 170 if hovered else 220, 10, CARD)
    animate_widget_color(card, "highlightbackground", border_target, 170 if hovered else 220, 10, BORDER)
    animate_widget_color(stripe, "bg", stripe_target, 150 if hovered else 210, 9, accent_color)
    for surface in surface_widgets:
        animate_widget_color(surface, "bg", surface_target, 170 if hovered else 220, 10, CARD)
    animate_grid_margin(card, max(3, base_margin - 2) if hovered else base_margin, 150 if hovered else 210)


def bind_card_animation(card, stripe, accent_color, surface_widgets, base_margin):
    card._card_hovered = False

    def enter(_event=None):
        leave_job = _card_leave_jobs.pop(id(card), None)
        if leave_job is not None:
            try:
                root.after_cancel(leave_job)
            except tk.TclError:
                pass
        if not getattr(card, "_card_hovered", False):
            animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, True)

    def delayed_leave():
        _card_leave_jobs.pop(id(card), None)
        if widget_exists(card) and not pointer_is_inside(card):
            animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, False)

    def leave(_event=None):
        old_job = _card_leave_jobs.pop(id(card), None)
        if old_job is not None:
            try:
                root.after_cancel(old_job)
            except tk.TclError:
                pass
        _card_leave_jobs[id(card)] = root.after(35, delayed_leave)

    def bind_tree(widget):
        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")
        for child in widget.winfo_children():
            bind_tree(child)

    bind_tree(card)


def animate_card_entrance(card, stripe, accent_color, delay):
    try:
        card.configure(highlightbackground=BG2)
        stripe.configure(bg=CARD2)
    except tk.TclError:
        return

    def begin():
        _anims.pop((id(card), "entrance"), None)
        if not widget_exists(card):
            return
        hovered = getattr(card, "_card_hovered", False)
        border_target = accent_color if hovered else BORDER
        stripe_target = lerp_color(accent_color, "#ffffff", 0.30) if hovered else accent_color
        animate_widget_color(card, "highlightbackground", border_target, 220, 10, BG2)
        animate_widget_color(stripe, "bg", stripe_target, 240, 11, CARD2)

    key = (id(card), "entrance")
    _anims[key] = root.after(delay, begin)


def pulse_widget(widget, base_color, pulse_color, cycles=2, duration=180):
    sequence = []
    for _ in range(cycles):
        sequence.extend((pulse_color, base_color))

    key = (id(widget), "pulse-sequence")

    def run(index=0):
        if index >= len(sequence) or not widget_exists(widget):
            _anims.pop(key, None)
            return
        animate_widget_color(widget, "bg", sequence[index], duration, 9, base_color)
        _anims[key] = root.after(duration, run, index + 1)

    run()


def fade_window(window, start, target, duration=220, on_complete=None):
    try:
        window.attributes("-alpha", start)
    except tk.TclError:
        if on_complete:
            on_complete()
        return

    def update(value):
        window.attributes("-alpha", value)

    def finish_later():
        if widget_exists(window) and on_complete:
            on_complete()

    animate_numeric(window, "window-alpha", start, target, update, duration, 12)
    if on_complete:
        root.after(duration + 20, finish_later)


def cancel_widget_tree_animations(parent):
    widget_ids = set()

    def collect(widget):
        widget_ids.add(id(widget))
        try:
            for child in widget.winfo_children():
                collect(child)
        except tk.TclError:
            pass

    collect(parent)
    for key in [key for key in _anims if key[0] in widget_ids]:
        animation_id = _anims.pop(key, None)
        _anim_tokens.pop(key, None)
        if animation_id is not None:
            try:
                root.after_cancel(animation_id)
            except (tk.TclError, ValueError):
                pass
    for card_id in [card_id for card_id in _card_leave_jobs if card_id in widget_ids]:
        leave_job = _card_leave_jobs.pop(card_id, None)
        if leave_job is not None:
            try:
                root.after_cancel(leave_job)
            except (tk.TclError, ValueError):
                pass


def cancel_all_animations():
    for animation_id in list(_anims.values()):
        try:
            root.after_cancel(animation_id)
        except (tk.TclError, ValueError):
            pass
    for leave_job in list(_card_leave_jobs.values()):
        try:
            root.after_cancel(leave_job)
        except (tk.TclError, ValueError):
            pass
    _anims.clear()
    _anim_tokens.clear()
    _card_leave_jobs.clear()

# ==========================================
# UI FUNCTIONS
# ==========================================
def get_icon(icon_filename, size=48):
    if not HAS_PIL or not icon_filename:
        return None
    size = max(24, int(round(size / 4) * 4))
    key = f"{icon_filename}-{size}"
    if key in icon_cache:
        return icon_cache[key]
    icon_path = os.path.join(ICONS_DIR, icon_filename)
    if not os.path.exists(icon_path):
        return None
    try:
        with Image.open(icon_path) as source:
            img = source.convert("RGBA").resize((size, size), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img)
        icon_cache[key] = tk_img
        while len(icon_cache) > 256:
            icon_cache.pop(next(iter(icon_cache)))
        return tk_img
    except (OSError, ValueError, TypeError, tk.TclError):
        return None


def resolve_card_art_source(item):
    """Return a user artwork path, or fall back to the launcher's cached icon."""
    artwork = str(item.get("artwork", "") or "").strip()
    if artwork:
        artwork = os.path.expanduser(os.path.expandvars(artwork))
        if not os.path.isabs(artwork):
            artwork = os.path.join(BASE_DIR, artwork)
        if os.path.isfile(artwork):
            return artwork, True
    icon_filename = str(item.get("icon", "") or "").strip()
    if icon_filename:
        icon_path = os.path.join(ICONS_DIR, icon_filename)
        if os.path.isfile(icon_path):
            return icon_path, False
    return "", False


def compose_card_backdrop(source_path, width, height, accent_color, is_artwork=False):
    """Build a muted card backdrop with a strong dark text-safety gradient."""
    if not HAS_PIL or not source_path:
        return None
    width = max(220, min(720, int(width)))
    height = max(88, min(220, int(height)))
    try:
        with Image.open(source_path) as source:
            source = source.convert("RGBA")
            base = Image.new("RGBA", (width, height), hex_to_rgb(CARD) + (255,))
            accent = hex_to_rgb(accent_color)

            if is_artwork:
                cover = ImageOps.fit(
                    source, (width, height), method=Image.LANCZOS,
                    centering=(0.5, 0.44),
                )
                cover = ImageEnhance.Color(cover).enhance(0.62)
                cover = ImageEnhance.Brightness(cover).enhance(0.48)
                cover = cover.filter(ImageFilter.GaussianBlur(0.65))
                base.alpha_composite(cover)
            else:
                glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                glow_draw = ImageDraw.Draw(glow_layer)
                glow_radius = int(height * 0.95)
                glow_x = width - int(height * 0.52)
                glow_y = height // 2
                glow_draw.ellipse(
                    (
                        glow_x - glow_radius, glow_y - glow_radius,
                        glow_x + glow_radius, glow_y + glow_radius,
                    ),
                    fill=accent + (74,),
                )
                glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(max(14, height // 4)))
                base.alpha_composite(glow_layer)

                icon_size = max(height, int(height * 1.42))
                icon = ImageOps.contain(source, (icon_size, icon_size), method=Image.LANCZOS)
                icon_alpha = icon.getchannel("A").point(lambda value: int(value * 0.25))
                icon.putalpha(icon_alpha)
                x = width - icon.width + int(height * 0.15)
                y = (height - icon.height) // 2
                base.alpha_composite(icon, (x, y))

            # This overlay is deliberately strongest where title/path text sits.
            readability = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            readability_draw = ImageDraw.Draw(readability)
            card_rgb = hex_to_rgb(CARD)
            for x in range(width):
                progress = x / max(1, width - 1)
                alpha = int(238 - (132 * progress))
                readability_draw.line((x, 0, x, height), fill=card_rgb + (alpha,))
            readability_draw.rectangle(
                (0, height - max(25, height // 4), width, height),
                fill=card_rgb + (118,),
            )
            base.alpha_composite(readability)

            # A quiet lower accent keeps icon-only previews from looking flat.
            detail = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            detail_draw = ImageDraw.Draw(detail)
            detail_draw.line(
                (int(width * 0.58), height - 1, width, height - 1),
                fill=accent + (96,), width=1,
            )
            base.alpha_composite(detail)
            return base.convert("RGB")
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def get_card_backdrop(item, width, height):
    """Return a bounded cached Tk image for the card's optional preview."""
    if not HAS_PIL or not launcher_settings.get("card_art_enabled", True):
        return None
    source_path, is_artwork = resolve_card_art_source(item)
    if not source_path:
        return None
    width = max(220, min(720, int(round(width / 8) * 8)))
    height = max(88, min(220, int(round(height / 4) * 4)))
    try:
        modified = os.path.getmtime(source_path)
    except OSError:
        return None
    key = (
        source_path, modified, width, height,
        str(item.get("color", ACCENT)), is_artwork,
    )
    if key in card_art_cache:
        return card_art_cache[key]
    composed = compose_card_backdrop(
        source_path, width, height, str(item.get("color", ACCENT)), is_artwork,
    )
    if composed is None:
        return None
    try:
        tk_image = ImageTk.PhotoImage(composed)
    except (RuntimeError, tk.TclError):
        return None
    card_art_cache[key] = tk_image
    while len(card_art_cache) > CARD_ART_CACHE_LIMIT:
        card_art_cache.pop(next(iter(card_art_cache)))
    return tk_image


def set_card_art_enabled(enabled):
    """Persist the optional backdrop preference and redraw visible cards."""
    launcher_settings["card_art_enabled"] = bool(enabled and HAS_PIL)
    save_launcher_settings()
    if root is not None and widget_exists(root):
        refresh()


def toggle_card_art_enabled():
    set_card_art_enabled(not launcher_settings.get("card_art_enabled", True))


def show_theme_menu(_event=None):
    """Expose theme color and the card-preview preference in one compact menu."""
    menu = tk.Menu(
        root, tearoff=0, bg=CARD2, fg=TEXT,
        activebackground=ACCENT, activeforeground=TEXT,
        font=("Segoe UI", 10), bd=0,
    )
    enabled = bool(launcher_settings.get("card_art_enabled", True) and HAS_PIL)
    menu.add_command(
        label=f"{'✓' if enabled else '  '}  Card artwork previews",
        command=toggle_card_art_enabled,
        state="normal" if HAS_PIL else "disabled",
    )
    menu.add_separator()
    menu.add_command(label="◈  Choose background color…", command=pick_bg_color)
    menu.add_separator()
    menu.add_command(
        label="◆  Data vault unlocked for this session",
        state="disabled",
    )
    try:
        x = bg_btn.winfo_rootx()
        y = bg_btn.winfo_rooty() + bg_btn.winfo_height() + 4
        menu.tk_popup(x, y)
    finally:
        try:
            menu.grab_release()
        except tk.TclError:
            pass


def pick_bg_color():
    global BG, BG2
    result = colorchooser.askcolor(color=BG, title="Choose Background Color")
    if result and result[1]:
        chosen = result[1]
        BG = chosen
        r, g, b = hex_to_rgb(chosen)
        BG2 = rgb_to_hex(min(255, r+10), min(255, g+10), min(255, b+15))
        for widget in (root, canvas, frame, tab_bar):
            animate_widget_color(widget, "bg", BG, 320, 14, chosen)
        refresh()


def format_activity_age(epoch):
    try:
        seconds = max(0, int(time.time() - float(epoch)))
    except (TypeError, ValueError):
        return "RECENT"
    if seconds < 60:
        return "NOW"
    if seconds < 3600:
        return f"{seconds // 60}M AGO"
    if seconds < 86400:
        return f"{seconds // 3600}H AGO"
    return f"{seconds // 86400}D AGO"


def update_activity_rail():
    if not widget_exists(activity_primary_lbl) or not widget_exists(activity_secondary_lbl):
        return
    with data_lock:
        entries = list(recent_activity[:2])
    if not entries:
        activity_primary_lbl.config(text="NO RECENT SIGNALS", fg=SUBTEXT)
        activity_secondary_lbl.config(text="Launch a game or app to begin tracking", fg=MUTED)
        return
    primary = entries[0]
    colors = {
        "critical": RED, "warning": ORANGE, "success": GREEN_HOVER,
        "error": RED, "info": NEON,
    }
    primary_text = f"{primary['title']}   ·   {format_activity_age(primary.get('epoch'))}"
    if len(primary_text) > 58:
        primary_text = primary_text[:57].rstrip() + "…"
    activity_primary_lbl.config(
        text=primary_text,
        fg=colors.get(primary.get("severity", "info"), NEON),
    )
    if len(entries) > 1:
        secondary = entries[1]
        secondary_text = f"{secondary['title']}   ·   {format_activity_age(secondary.get('epoch'))}"
    else:
        secondary_text = primary.get("detail") or "Activity monitoring online"
    if len(secondary_text) > 64:
        secondary_text = secondary_text[:63].rstrip() + "…"
    activity_secondary_lbl.config(text=secondary_text, fg=SUBTEXT)


def update_scale(event=None):
    global current_scale, resize_job
    if event is not None and event.widget is not root:
        return
    width = root.winfo_width()
    new_scale = max(0.7, min(width / 950, 1.8))
    if abs(new_scale - current_scale) <= 0.05:
        return
    current_scale = new_scale
    if resize_job is not None:
        try:
            root.after_cancel(resize_job)
        except tk.TclError:
            pass
    resize_job = root.after(120, apply_resized_layout)


def apply_resized_layout():
    global resize_job
    resize_job = None
    apply_topbar_scaling()
    refresh()

def apply_topbar_scaling():
    width = root.winfo_width()
    font_size = 9 if width < 900 else (10 if width < 1350 else 11)
    sort_labels = {"pinned": "PINNED FIRST", "name": "NAME  A–Z", "playtime": "MOST PLAYED"}
    if width < 900:
        search_width, name_width = 8, 7
        scan_btn.config(text="⌁  SCAN", padx=8)
        add_btn.config(text="＋  ADD", padx=8)
        sort_btn.config(text="⇅  SORT", padx=8)
    elif width < 1350:
        search_width, name_width = 18, 15
        scan_btn.config(text="⌁  SCAN SYSTEM", padx=15)
        add_btn.config(text="＋  ADD ENTRY", padx=18)
        sort_btn.config(text=f"⇅  {sort_labels[current_sort]}", padx=15)
    else:
        search_width, name_width = 22, 19
        scan_btn.config(text="⌁  SCAN SYSTEM", padx=18)
        add_btn.config(text="＋  ADD ENTRY", padx=18)
        sort_btn.config(text=f"⇅  {sort_labels[current_sort]}", padx=15)
    search_entry.config(font=("Segoe UI", font_size), width=search_width)
    name_entry.config(font=("Segoe UI", font_size), width=name_width)
    scan_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))
    add_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))
    sort_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))

def current_view_items():
    if active_tab in ("reports", "monitor"):
        return []
    query = search_var.get().strip().casefold()
    with data_lock:
        items = list(current_list())
    if query:
        items = [item for item in items if query in item["name"].casefold()]
    return sort_items(items)


def search_games(*_):
    global search_job
    page_by_tab[active_tab] = 0
    if search_job is not None:
        try:
            root.after_cancel(search_job)
        except tk.TclError:
            pass
    search_job = root.after(120, apply_search)


def apply_search():
    global search_job
    search_job = None
    canvas.yview_moveto(0)
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def refresh(reset_page=False):
    if reset_page:
        page_by_tab[active_tab] = 0
    update_stats()
    update_activity_rail()
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def update_stats():
    with data_lock:
        total_g = len(games)
        total_a = len(apps)
        total_time = sum(item["playtime"] for item in games + apps)
    stats_lbl.config(text=f"{total_g} GAMES   ·   {total_a} APPS   ·   {format_time(total_time)} TOTAL ACTIVITY")
    compact_tabs = root.winfo_width() < 920
    if compact_tabs:
        games_tab_btn.config(text=f"GAMES {total_g}", padx=10)
        apps_tab_btn.config(text=f"APPS {total_a}", padx=10)
        founded_tab_btn.config(text=f"FOUND {len(founded)}", padx=10)
        reports_tab_btn.config(text=f"REPORTS {len(reports)}", padx=10)
        monitor_tab_btn.config(text="MONITOR", padx=10)
    else:
        games_tab_btn.config(text=f"◉  GAMES   {total_g}", padx=22)
        apps_tab_btn.config(text=f"◇  WORKSPACE   {total_a}", padx=22)
        founded_tab_btn.config(text=f"✦  DISCOVERED   {len(founded)}", padx=18)
        reports_tab_btn.config(text=f"△  REPORTS   {len(reports)}", padx=18)
        monitor_tab_btn.config(text="⌁  MONITOR", padx=18)
    if header_canvas is not None and header_stats_id is not None:
        try:
            header_canvas.itemconfigure(
                header_stats_id,
                text=f"{total_g:02d} GAMES     {total_a:02d} APPS     {len(reports):02d} REPORTS     {format_time(total_time).upper()} ACTIVE",
            )
        except tk.TclError:
            pass


def change_page(delta):
    page_by_tab[active_tab] = max(0, page_by_tab[active_tab] + delta)
    canvas.yview_moveto(0)
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def change_sort():
    global current_sort
    sorts = ["pinned", "name", "playtime"]
    idx = sorts.index(current_sort)
    current_sort = sorts[(idx + 1) % len(sorts)]
    page_by_tab[active_tab] = 0
    apply_topbar_scaling()
    refresh()


def set_library_controls_visible(visible):
    if visible:
        if not drop_container.winfo_manager():
            drop_container.pack(fill="x", pady=(0, 10), before=container)
        if not topbar.winfo_manager():
            topbar.pack(fill="x", padx=30, pady=(0, 14), before=container)
    else:
        drop_container.pack_forget()
        topbar.pack_forget()


def switch_tab(tab):
    global active_tab, search_job
    if tab not in page_by_tab:
        return
    active_tab = tab
    page_by_tab[tab] = 0
    animate_tab_state(games_tab_btn, tab == "games")
    animate_tab_state(apps_tab_btn, tab == "apps")
    animate_tab_state(founded_tab_btn, tab == "founded")
    animate_tab_state(reports_tab_btn, tab == "reports")
    animate_tab_state(monitor_tab_btn, tab == "monitor")
    set_library_controls_visible(tab not in ("reports", "monitor"))
    if tab == "monitor":
        request_hardware_monitor_start()
    else:
        cancel_hardware_monitor_view_refresh()
        schedule_hardware_monitor_idle_stop()
    if search_var.get():
        search_var.set("")
        if search_job is not None:
            try:
                root.after_cancel(search_job)
            except tk.TclError:
                pass
            search_job = None
    canvas.yview_moveto(0)
    refresh()

def show_context_menu(event, item):
    menu = tk.Menu(root, tearoff=0, bg=CARD2, fg=TEXT, activebackground=ACCENT, activeforeground="white", font=("Segoe UI", 10))
    menu.add_command(label="✏️ Rename", command=lambda: rename_item(item))
    menu.add_command(label="📁 Open Location", command=lambda: open_file_location(item))
    menu.add_command(label="↻ Change Location", command=lambda: change_item_location(item))
    menu.add_command(
        label="＋ Add Icon", command=lambda: add_custom_icon(item),
        state="normal" if HAS_PIL else "disabled",
    )
    if active_tab in ("games", "apps"):
        menu.add_command(
            label="■ End Task",
            command=lambda: end_task(item),
            state="normal" if is_item_running(item) else "disabled",
        )
    menu.add_separator()
    mv_txt = "Move to Apps" if active_tab == "games" else "Move to Games"
    menu.add_command(label=f"➡️ {mv_txt}", command=lambda: move_item(item))
    menu.add_command(label="❌ Delete", command=lambda: delete_item(item), foreground=RED)
    menu.tk_popup(event.x_root, event.y_root)

def make_founded_action_btn(parent, item):
    btn_row = tk.Frame(parent, bg=CARD)
    btn_row.pack(fill="x")

    def make_btn(text, bg_idle, fg_idle, bg_hover, fg_hover, cmd):
        b = tk.Button(btn_row, text=text, bg=bg_idle, fg=fg_idle, relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2", pady=4, bd=0, command=cmd)
        return bind_animated_button(b, bg_idle, bg_hover, fg_idle, fg_hover)

    make_btn("＋  GAMES", CARD2, SUBTEXT, ACCENT, TEXT, lambda: move_to_games(item)).pack(side="left", expand=True, fill="x", padx=(0, 3))
    make_btn("＋  WORKSPACE", CARD2, SUBTEXT, GREEN, TEXT, lambda: move_to_apps(item)).pack(side="left", expand=True, fill="x", padx=(3, 0))
    return btn_row


def make_action_btn(parent, text, bg_idle, fg_idle, bg_hover, fg_hover, cmd, scale=1.0):
    b = tk.Button(parent, text=text, bg=bg_idle, fg=fg_idle, relief="flat", font=("Segoe UI", max(7, int(7 * scale)), "bold"), cursor="hand2", pady=max(4, int(4 * scale)), bd=0, command=cmd)
    return bind_animated_button(b, bg_idle, bg_hover, fg_idle, fg_hover)

def compact_display_path(path, limit=46):
    clean = clean_path(path)
    parent = ntpath.basename(ntpath.dirname(clean))
    filename = ntpath.basename(clean)
    value = f"{parent}  /  {filename}" if parent else filename
    if len(value) <= limit:
        return value
    left = max(10, limit // 2 - 2)
    right = max(10, limit - left - 3)
    return f"{value[:left]}...{value[-right:]}"


def bind_context_tree(widget, item):
    widget.bind("<Button-3>", lambda event, entry=item: show_context_menu(event, entry), add="+")
    for child in widget.winfo_children():
        bind_context_tree(child, item)


def clear_item_widgets():
    cancel_widget_tree_animations(frame)
    for widget in frame.winfo_children():
        widget.destroy()


def sys_report_window_exists():
    try:
        return sys_report_window is not None and bool(sys_report_window.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def request_system_report_cancel():
    if not sys_report_running:
        return
    sys_report_cancel.set()
    if widget_exists(sys_report_status_lbl):
        sys_report_status_lbl.config(text="CANCELLING DIAGNOSTIC...", fg=ORANGE)


def create_system_report_window():
    global sys_report_window, sys_report_status_lbl, sys_report_detail_lbl, sys_report_progress
    sys_report_window = tk.Toplevel(root)
    sys_report_window.title("XVVIIX SYS REPORT")
    sys_report_window.geometry("720x490")
    sys_report_window.minsize(640, 450)
    sys_report_window.configure(bg=BG)
    sys_report_window.transient(root)
    sys_report_window.grab_set()
    sys_report_window.protocol("WM_DELETE_WINDOW", request_system_report_cancel)
    enable_win11_round_corners(sys_report_window)
    tk.Frame(sys_report_window, bg=CYAN, height=4).pack(fill="x")
    body = tk.Frame(sys_report_window, bg=BG, padx=34, pady=26)
    body.pack(fill="both", expand=True)
    tk.Label(
        body, text="△  SYS REPORT  //  FULL SPECTRUM DIAGNOSTIC",
        bg=BG, fg=TEXT, font=("Segoe UI Black", 16, "bold"),
    ).pack(anchor="w")
    tk.Label(
        body, text="LOCAL HARDWARE · OS · STORAGE · NETWORK · SECURITY TELEMETRY",
        bg=BG, fg=NEON, font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(5, 22))
    sys_report_status_lbl = tk.Label(
        body, text="INITIALIZING", bg=BG, fg=CYAN,
        font=("Consolas", 12, "bold"), anchor="w",
    )
    sys_report_status_lbl.pack(fill="x")
    sys_report_detail_lbl = tk.Label(
        body, text="Preparing diagnostic pipeline", bg=BG, fg=SUBTEXT,
        font=("Segoe UI", 9), anchor="w",
    )
    sys_report_detail_lbl.pack(fill="x", pady=(5, 14))
    sys_report_progress = tk.Canvas(
        body, height=18, bg=CARD2, highlightthickness=1,
        highlightbackground=BORDER, bd=0,
    )
    sys_report_progress.pack(fill="x")
    sys_report_progress.fill_id = sys_report_progress.create_rectangle(0, 0, 0, 18, fill=CYAN, outline="")
    sys_report_progress.text_id = sys_report_progress.create_text(
        10, 9, text="0%", anchor="w", fill=TEXT,
        font=("Consolas", 7, "bold"),
    )
    phases = tk.Frame(body, bg=CARD, padx=14, pady=12, highlightbackground=BORDER_SOFT, highlightthickness=1)
    phases.pack(fill="both", expand=True, pady=(18, 16))
    phase_text = (
        "01  COMMAND PLATFORM        04  GRAPHICS SUBSYSTEM\n"
        "02  COMPUTE CORE            05  STORAGE MODULES\n"
        "03  MEMORY ARRAY            06  NETWORK LINKS\n"
        "07  POWER / THERMAL         08  SECURITY PERIMETER\n"
        "09  XVVIIX ENVIRONMENT       10  HEALTH ANALYSIS"
    )
    tk.Label(
        phases, text=phase_text, bg=CARD, fg=SUBTEXT,
        font=("Consolas", 9, "bold"), justify="left",
    ).pack(anchor="w")
    cancel_button = tk.Button(
        body, text="CANCEL REPORT", bg=CARD2, fg=SUBTEXT,
        relief="flat", bd=0, padx=18, pady=8, cursor="hand2",
        font=("Segoe UI", 8, "bold"), command=request_system_report_cancel,
    )
    cancel_button.pack(anchor="e")
    bind_animated_button(cancel_button, CARD2, RED, SUBTEXT, TEXT)
    fade_window(sys_report_window, 0.0, 1.0, 220)


def update_system_report_progress(percent, phase, detail=""):
    if not sys_report_window_exists():
        return
    if widget_exists(sys_report_status_lbl):
        sys_report_status_lbl.config(text=str(phase).upper(), fg=CYAN)
    if widget_exists(sys_report_detail_lbl):
        sys_report_detail_lbl.config(text=str(detail)[:120])
    if widget_exists(sys_report_progress):
        sys_report_progress.update_idletasks()
        width = max(1, sys_report_progress.winfo_width())
        fill_width = int(width * max(0, min(100, int(percent))) / 100)
        sys_report_progress.coords(sys_report_progress.fill_id, 0, 0, fill_width, 18)
        sys_report_progress.itemconfigure(sys_report_progress.text_id, text=f"{int(percent):02d}%")


def close_system_report_window():
    global sys_report_window
    if not sys_report_window_exists():
        sys_report_window = None
        return
    try:
        sys_report_window.grab_release()
        sys_report_window.destroy()
    except tk.TclError:
        pass
    sys_report_window = None


def finish_system_report(report):
    global sys_report_running, selected_report_id
    sys_report_running = False
    close_system_report_window()
    if report is None:
        messagebox.showerror("SYS REPORT", "The report could not be stored in the encrypted report library.")
        return
    selected_report_id = report.get("id")
    add_activity(
        "system_report", f"SYS REPORT complete · {report.get('health_score', 0)}/100",
        detail=report.get("details", "Local diagnostic complete"),
        severity=report.get("severity", "info"),
    )
    switch_tab("reports")


def fail_system_report(message, cancelled=False):
    global sys_report_running
    sys_report_running = False
    close_system_report_window()
    if not cancelled:
        messagebox.showerror("SYS REPORT", f"The diagnostic could not be completed:\n\n{message}")


def start_system_report():
    global sys_report_running
    if sys_report_running:
        if sys_report_window_exists():
            sys_report_window.deiconify()
            sys_report_window.lift()
        return
    if vault_key is None:
        messagebox.showerror("SYS REPORT", "Unlock the XVVIIX data vault before creating a report.")
        return
    sys_report_running = True
    sys_report_cancel.clear()
    create_system_report_window()

    def progress(percent, phase, detail):
        post_ui(update_system_report_progress, percent, phase, detail)

    def worker():
        try:
            raw_report = collect_system_report(progress)
            if sys_report_cancel.is_set():
                raise SystemReportCancelled()
            stored = add_report(raw_report)
            post_ui(finish_system_report, stored)
        except SystemReportCancelled:
            post_ui(fail_system_report, "Cancelled", True)
        except Exception as exc:
            LOG.exception("SYS REPORT failed")
            post_ui(fail_system_report, str(exc), False)

    threading.Thread(target=worker, daemon=True, name="sys-report-scanner").start()


def report_severity_color(severity):
    return {
        "critical": RED,
        "error": RED,
        "warning": ORANGE,
        "info": CYAN,
        "success": GREEN,
    }.get(str(severity).lower(), CYAN)


def format_report_timestamp(report):
    timestamp = str(report.get("timestamp", "") or "")
    if "T" in timestamp:
        timestamp = timestamp.replace("T", " ", 1)
    if "+" in timestamp:
        timestamp = timestamp.rsplit("+", 1)[0]
    return timestamp[:19] or "TIME UNAVAILABLE"


def format_report_text(report):
    if report.get("kind") == "system_report":
        lines = [
            "XVVIIX SYS REPORT",
            f"Report ID: {report.get('id', '')}",
            f"Time: {report.get('timestamp', '')}",
            f"Device: {report.get('item_name', '')}",
            f"Health score: {report.get('health_score', 0)}/100",
            f"Scan duration: {report.get('scan_duration_ms', 0) / 1000:.1f}s",
            "",
            "SUMMARY",
        ]
        for label, value in report.get("summary", {}).items():
            lines.append(f"{label.upper()}: {value}")
        lines.extend(("", "FINDINGS"))
        for finding in report.get("findings", []):
            lines.append(f"[{finding.get('severity', 'info').upper()}] {finding.get('title', '')}")
            if finding.get("detail"):
                lines.append(f"  {finding['detail']}")
            if finding.get("action"):
                lines.append(f"  Action: {finding['action']}")
        for section in report.get("sections", []):
            lines.extend(("", section.get("title", "SECTION")))
            for item in section.get("items", []):
                lines.append(f"{item.get('label', '')}: {item.get('value', '')}")
        return "\n".join(lines) + "\n"

    suggestions = report.get("suggestions", [])
    suggestion_text = "\n".join(f"- {value}" for value in suggestions) or "- Review the game's own logs."
    return (
        f"XVVIIX CRASH REPORT\n"
        f"Report ID: {report.get('id', '')}\n"
        f"Time: {report.get('timestamp', '')}\n"
        f"Game: {report.get('item_name', '')}\n"
        f"Path: {report.get('item_path', '')}\n"
        f"PID: {report.get('pid', 0)}\n"
        f"Exit: {report.get('exit_hex') or report.get('exit_code')}\n"
        f"Runtime: {format_time(report.get('runtime_seconds', 0))}\n"
        f"Cause: {report.get('cause', '')}\n\n"
        f"Analysis:\n{report.get('details', '')}\n\n"
        f"Recommended actions:\n{suggestion_text}\n"
    )


def copy_report(report):
    try:
        root.clipboard_clear()
        root.clipboard_append(format_report_text(report))
        root.update_idletasks()
        add_activity(
            "report", f"Report copied · {report.get('item_name', 'Game')}",
            detail=report.get("cause", "Crash report"), severity="info",
        )
    except tk.TclError as exc:
        messagebox.showerror("Copy Report", f"Could not copy the report:\n{exc}")


def delete_report(report):
    global selected_report_id
    if not messagebox.askyesno(
        "Delete Report", f"Delete the report for {report.get('item_name', 'this game')}?",
    ):
        return
    report_id = report.get("id")
    with data_lock:
        reports[:] = [entry for entry in reports if entry.get("id") != report_id]
    _save(REPORTS_FILE, reports)
    if selected_report_id == report_id:
        selected_report_id = None
    refresh(reset_page=True)


def clear_all_reports():
    global selected_report_id
    if not reports:
        return
    if not messagebox.askyesno(
        "Clear Reports", "Delete every saved crash report?\n\nThis cannot be undone.",
        icon="warning",
    ):
        return
    with data_lock:
        reports.clear()
    _save(REPORTS_FILE, reports)
    selected_report_id = None
    refresh(reset_page=True)


def set_report_filter(value):
    global report_filter, selected_report_id
    if value not in ("all", "system", "crash"):
        return
    report_filter = value
    selected_report_id = None
    page_by_tab["reports"] = 0
    display_reports()


def view_report(report):
    global selected_report_id
    selected_report_id = report.get("id")
    canvas.yview_moveto(0)
    display_reports()


def show_previous_reports():
    global selected_report_id
    selected_report_id = None
    canvas.yview_moveto(0)
    display_reports()


def display_system_report_detail(report):
    clear_item_widgets()
    frame.grid_columnconfigure(0, weight=1)
    frame.grid_columnconfigure(1, weight=1)
    compact = root.winfo_width() < 980
    score = int(report.get("health_score", 0))
    severity_color = report_severity_color(report.get("severity"))

    header = tk.Frame(
        frame, bg=CARD, padx=22, pady=18,
        highlightbackground=BORDER, highlightthickness=1,
    )
    header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 10))
    heading = tk.Frame(header, bg=CARD)
    heading.pack(side="left", fill="both", expand=True)
    tk.Label(
        heading, text="△  XVVIIX SYS REPORT  //  MISSION SYSTEMS",
        bg=CARD, fg=TEXT, font=("Segoe UI Black", 15 if not compact else 12, "bold"),
    ).pack(anchor="w")
    tk.Label(
        heading,
        text=f"REPORT {report.get('id', '')[:12].upper()}   ·   {format_report_timestamp(report)}   ·   {report.get('item_name', '').upper()}",
        bg=CARD, fg=NEON, font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(5, 0))
    score_panel = tk.Frame(header, bg=CARD2, padx=18, pady=8)
    score_panel.pack(side="right", padx=(16, 0))
    tk.Label(score_panel, text=f"{score:02d}", bg=CARD2, fg=severity_color, font=("Consolas", 24, "bold")).pack()
    tk.Label(score_panel, text="HEALTH / 100", bg=CARD2, fg=SUBTEXT, font=("Consolas", 7, "bold")).pack()

    actions = tk.Frame(frame, bg=BG)
    actions.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    make_action_btn(
        actions, "←  PREVIOUS REPORTS", CARD2, SUBTEXT, ACCENT, TEXT,
        show_previous_reports,
    ).pack(side="left")
    make_action_btn(
        actions, "COPY FULL REPORT", CARD2, SUBTEXT, CYAN, TEXT,
        lambda: copy_report(report),
    ).pack(side="right", padx=(6, 0))
    make_action_btn(
        actions, "DELETE", CARD2, MUTED, RED, TEXT,
        lambda: delete_report(report),
    ).pack(side="right")

    summary = tk.Frame(
        frame, bg=BG2, padx=14, pady=12,
        highlightbackground=BORDER_SOFT, highlightthickness=1,
    )
    summary.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    summary_values = list(report.get("summary", {}).items())
    for index, (label, value) in enumerate(summary_values[:8]):
        cell = tk.Frame(summary, bg=CARD, padx=10, pady=8)
        cell.grid(row=index // (2 if compact else 4), column=index % (2 if compact else 4), sticky="nsew", padx=3, pady=3)
        tk.Label(cell, text=label.upper(), bg=CARD, fg=MUTED, font=("Consolas", 7, "bold")).pack(anchor="w")
        tk.Label(cell, text=value, bg=CARD, fg=TEXT, font=("Segoe UI", 8, "bold"), wraplength=220 if compact else 190, justify="left").pack(anchor="w", pady=(3, 0))
    for column in range(2 if compact else 4):
        summary.grid_columnconfigure(column, weight=1, uniform="sys-summary")

    findings = report.get("findings", [])
    finding_panel = tk.Frame(
        frame, bg=CARD, padx=16, pady=12,
        highlightbackground=BORDER_SOFT, highlightthickness=1,
    )
    finding_panel.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    tk.Label(
        finding_panel, text=f"HEALTH ANALYSIS  //  {len(findings):02d} FINDINGS",
        bg=CARD, fg=NEON, font=("Consolas", 9, "bold"),
    ).pack(anchor="w", pady=(0, 7))
    for finding in findings[:12]:
        color = report_severity_color(finding.get("severity"))
        row = tk.Frame(finding_panel, bg=CARD2, padx=9, pady=7)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="●", bg=CARD2, fg=color, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 7))
        text = finding.get("title", "")
        if finding.get("detail"):
            text += f"  —  {finding['detail']}"
        if finding.get("action"):
            text += f"  //  ACTION: {finding['action']}"
        tk.Label(row, text=text, bg=CARD2, fg="#c4d1e5", font=("Segoe UI", 8), wraplength=max(450, root.winfo_width() - 160), justify="left", anchor="w").pack(side="left", fill="x", expand=True)

    sections = report.get("sections", [])
    section_start = 4
    columns = 1 if compact else 2
    for index, section in enumerate(sections):
        section_color = report_severity_color(section.get("status"))
        panel = tk.Frame(
            frame, bg=CARD, padx=14, pady=12,
            highlightbackground=BORDER_SOFT, highlightthickness=1,
        )
        panel.grid(
            row=section_start + index // columns,
            column=index % columns,
            columnspan=2 if compact else 1,
            sticky="nsew", padx=10 if compact else (10, 5) if index % 2 == 0 else (5, 10), pady=6,
        )
        top = tk.Frame(panel, bg=CARD)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text=section.get("title", "SYSTEM"), bg=CARD, fg=TEXT, font=("Consolas", 9, "bold")).pack(side="left")
        tk.Label(top, text="●", bg=CARD, fg=section_color, font=("Segoe UI", 9, "bold")).pack(side="right")
        for item in section.get("items", []):
            row = tk.Frame(panel, bg=CARD2, padx=8, pady=5)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=item.get("label", ""), bg=CARD2, fg=MUTED, font=("Consolas", 7, "bold"), width=19, anchor="w").pack(side="left")
            tk.Label(
                row, text=item.get("value", ""), bg=CARD2,
                fg=report_severity_color(item.get("status")) if item.get("status") in ("warning", "critical", "error") else "#c4d1e5",
                font=("Segoe UI", 8), anchor="w", justify="left",
                wraplength=360 if compact else 300,
            ).pack(side="left", fill="x", expand=True)


def _monitor_percent(value):
    if monitor_clamp_percent is None:
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError, OverflowError):
            return None
    return monitor_clamp_percent(value)


def _monitor_bytes(value):
    return monitor_format_bytes(value) if monitor_format_bytes is not None else "--"


def _monitor_rate(value):
    return monitor_format_rate(value) if monitor_format_rate is not None else "--"


def _monitor_uptime(seconds):
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "--"
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def cancel_hardware_monitor_idle_stop():
    global hardware_monitor_idle_job
    if hardware_monitor_idle_job is not None and root is not None:
        try:
            root.after_cancel(hardware_monitor_idle_job)
        except (tk.TclError, ValueError):
            pass
    hardware_monitor_idle_job = None


def _finish_hardware_monitor_idle_stop():
    global hardware_monitor_state
    hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
    if active_tab == "monitor" or monitor_overlay_requested:
        request_hardware_monitor_start()


def _stop_hardware_monitor_if_idle():
    global hardware_monitor, hardware_monitor_state, hardware_monitor_idle_job
    hardware_monitor_idle_job = None
    if active_tab == "monitor" or widget_exists(monitor_overlay):
        return
    service = hardware_monitor
    hardware_monitor = None
    if service is None:
        hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
        return
    hardware_monitor_state = "stopping"

    def worker():
        service.stop()
        post_ui(_finish_hardware_monitor_idle_stop)

    threading.Thread(
        target=worker, daemon=True, name="hardware-monitor-stopper"
    ).start()


def schedule_hardware_monitor_idle_stop():
    global hardware_monitor_idle_job
    cancel_hardware_monitor_idle_stop()
    if root is not None and hardware_monitor is not None:
        hardware_monitor_idle_job = root.after(6000, _stop_hardware_monitor_if_idle)


def _finish_hardware_monitor_start(service, error=""):
    global hardware_monitor, hardware_monitor_state, hardware_monitor_error
    global monitor_overlay_requested
    if monitor_stop.is_set() or root is None or not widget_exists(root):
        if service is not None:
            service.stop()
        return
    if error:
        hardware_monitor = None
        hardware_monitor_state = "failed"
        hardware_monitor_error = str(error)[:240]
        LOG.warning("Integrated Hardware Monitor unavailable: %s", error)
    else:
        hardware_monitor = service
        hardware_monitor_state = "ready"
        hardware_monitor_error = ""
    if active_tab == "monitor":
        display_hardware_monitor()
    if monitor_overlay_requested:
        monitor_overlay_requested = False
        if hardware_monitor is not None:
            open_hardware_overlay()
    if active_tab != "monitor" and not widget_exists(monitor_overlay):
        schedule_hardware_monitor_idle_stop()


def request_hardware_monitor_start():
    """Initialize expensive GPU/process telemetry off the Tk thread, on demand."""
    global hardware_monitor_state, hardware_monitor_error
    cancel_hardware_monitor_idle_stop()
    if hardware_monitor is not None and hardware_monitor.is_running():
        hardware_monitor_state = "ready"
        return True
    if hardware_monitor_state == "starting":
        return False
    if hardware_monitor_state == "stopping":
        if root is not None:
            root.after(120, request_hardware_monitor_start)
        return False
    if not HAS_HARDWARE_MONITOR:
        hardware_monitor_state = "unavailable"
        hardware_monitor_error = "psutil is not installed"
        return False
    hardware_monitor_state = "starting"
    hardware_monitor_error = ""

    def worker():
        service = None
        error = ""
        try:
            service = HardwareMonitorService(interval=1.0)
            service.start()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if service is not None:
                service.stop()
            service = None
        post_ui(_finish_hardware_monitor_start, service, error)

    threading.Thread(
        target=worker, daemon=True, name="hardware-monitor-loader"
    ).start()
    return False


def _monitor_metric_card(parent, column, title, accent):
    card = tk.Frame(
        parent, bg=CARD, padx=15, pady=13,
        highlightbackground=BORDER_SOFT, highlightthickness=1,
    )
    card.grid(row=0, column=column, sticky="nsew", padx=5, pady=5)
    tk.Label(
        card, text=title, bg=CARD, fg=accent,
        font=("Consolas", 8, "bold"), anchor="w",
    ).pack(fill="x")
    value = tk.Label(
        card, text="--", bg=CARD, fg=TEXT,
        font=("Segoe UI Black", 22, "bold"), anchor="w",
    )
    value.pack(fill="x", pady=(3, 1))
    detail = tk.Label(
        card, text="Waiting for telemetry", bg=CARD, fg=SUBTEXT,
        font=("Segoe UI", 8), anchor="w", justify="left",
        wraplength=300 if root.winfo_width() >= 980 else 280,
    )
    detail.pack(fill="x")
    chart = tk.Canvas(card, height=38, bg=CARD2, highlightthickness=0, bd=0)
    chart.pack(fill="x", pady=(10, 0))
    return {"card": card, "value": value, "detail": detail, "chart": chart, "accent": accent}


def _draw_monitor_history(chart, values, color):
    if not widget_exists(chart):
        return
    chart.delete("all")
    chart.update_idletasks()
    width = max(40, chart.winfo_width())
    height = max(20, chart.winfo_height())
    chart.create_line(0, height - 1, width, height - 1, fill=BORDER, width=1)
    chart.create_line(0, height // 2, width, height // 2, fill=BORDER_SOFT, width=1)
    samples = list(values)
    if not samples:
        return
    if len(samples) == 1:
        samples.insert(0, samples[0])
    step = width / max(1, len(samples) - 1)
    points = []
    for index, raw_value in enumerate(samples):
        value = _monitor_percent(raw_value) or 0.0
        points.extend((index * step, height - (value / 100.0 * (height - 3)) - 1))
    chart.create_line(*points, fill=color, width=2, smooth=True)


def cancel_hardware_monitor_view_refresh():
    global monitor_refresh_job
    if monitor_refresh_job is not None and root is not None:
        try:
            root.after_cancel(monitor_refresh_job)
        except (tk.TclError, ValueError):
            pass
    monitor_refresh_job = None


def _schedule_hardware_monitor_view_refresh():
    global monitor_refresh_job
    cancel_hardware_monitor_view_refresh()
    if root is not None and active_tab == "monitor":
        monitor_refresh_job = root.after(900, update_hardware_monitor_view)


def set_monitor_process_filter(*_args):
    global monitor_process_filter
    monitor_process_filter = (
        monitor_process_query_var.get().strip().casefold()
        if monitor_process_query_var is not None else ""
    )
    update_hardware_monitor_view(force=True)


def set_monitor_process_sort(sort_key):
    global monitor_process_sort
    if sort_key not in {"cpu", "memory", "name", "pid"}:
        return
    monitor_process_sort = sort_key
    for key, button in monitor_ui.get("sort_buttons", {}).items():
        selected = key == sort_key
        button.config(bg=ACCENT if selected else CARD2, fg=TEXT if selected else SUBTEXT)
        button._anim_idle_bg = ACCENT if selected else CARD2
        button._anim_idle_fg = TEXT if selected else SUBTEXT
    update_hardware_monitor_view(force=True)


def _selected_monitor_process(_event=None):
    tree = monitor_ui.get("process_tree")
    detail = monitor_ui.get("process_detail")
    if not widget_exists(tree) or not widget_exists(detail):
        return
    selected = tree.selection()
    if not selected:
        detail.config(text="Select a process to inspect its user and executable path.")
        return
    record = monitor_ui.get("process_records", {}).get(selected[0])
    if not record:
        return
    started = "--"
    if record.get("create_time"):
        try:
            started = datetime.fromtimestamp(record["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, OverflowError):
            pass
    path = record.get("executable") or "Executable path unavailable"
    detail.config(
        text=f"USER  {record.get('username') or 'Unavailable'}   ·   STARTED  {started}   ·   PATH  {path}"
    )


def _update_monitor_process_table(processes, user_total, truncated=False):
    tree = monitor_ui.get("process_tree")
    count_label = monitor_ui.get("process_count")
    if not widget_exists(tree):
        return
    query = monitor_process_filter
    filtered = []
    for process in processes:
        search_text = " ".join((
            str(process.get("name", "")), str(process.get("pid", "")),
            str(process.get("username", "")), str(process.get("executable", "")),
        )).casefold()
        if not query or query in search_text:
            filtered.append(process)
    if monitor_process_sort == "memory":
        filtered.sort(key=lambda item: (item.get("memory_bytes", 0), item.get("cpu_percent", 0)), reverse=True)
    elif monitor_process_sort == "name":
        filtered.sort(key=lambda item: (str(item.get("name", "")).casefold(), item.get("pid", 0)))
    elif monitor_process_sort == "pid":
        filtered.sort(key=lambda item: item.get("pid", 0))
    else:
        filtered.sort(key=lambda item: (item.get("cpu_percent", 0), item.get("memory_bytes", 0)), reverse=True)

    selected = set(tree.selection())
    expected = []
    record_map = {}
    for index, process in enumerate(filtered):
        pid = max(0, int(process.get("pid", 0)))
        iid = f"process-{pid}"
        expected.append(iid)
        record_map[iid] = process
        values = (
            str(process.get("name") or "Unknown process")[:80],
            pid,
            f"{_monitor_percent(process.get('cpu_percent')) or 0.0:.1f}%",
            _monitor_bytes(process.get("memory_bytes")),
            max(0, int(process.get("threads", 0))),
            str(process.get("status") or "unknown").upper(),
        )
        tag = "even" if index % 2 == 0 else "odd"
        if tree.exists(iid):
            tree.item(iid, values=values, tags=(tag,))
        else:
            tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        tree.move(iid, "", index)
    for iid in set(tree.get_children()) - set(expected):
        tree.delete(iid)
    monitor_ui["process_records"] = record_map
    for iid in selected & set(expected):
        tree.selection_add(iid)
    if widget_exists(count_label):
        suffix = " · SAFETY LIMIT ACTIVE" if truncated else ""
        count_label.config(
            text=f"SHOWING {len(filtered):03d} / {int(user_total):03d} CURRENT-USER PROCESSES{suffix}"
        )
    _selected_monitor_process()


def display_hardware_monitor():
    global monitor_process_query_var
    cancel_hardware_monitor_view_refresh()
    clear_item_widgets()
    monitor_ui.clear()
    for column in range(4):
        frame.grid_columnconfigure(column, weight=1 if column == 0 else 0, uniform="")

    header = tk.Frame(
        frame, bg=CARD, padx=22, pady=16,
        highlightbackground=BORDER, highlightthickness=1,
    )
    header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 6))
    compact_header = root.winfo_width() < 900
    heading = tk.Frame(header, bg=CARD)
    if compact_header:
        heading.pack(fill="x")
    else:
        heading.pack(side="left", fill="both", expand=True)
    tk.Label(
        heading, text="⌁  HARDWARE MONITOR  //  LIVE TELEMETRY",
        bg=CARD, fg=TEXT, font=("Segoe UI Black", 16, "bold"), anchor="w",
    ).pack(fill="x")
    tk.Label(
        heading,
        text="NORMALIZED 0–100% METRICS  ·  CURRENT-USER PROCESS INVENTORY  ·  LOCAL-ONLY SAMPLING",
        bg=CARD, fg=NEON, font=("Consolas", 8, "bold"), anchor="w",
    ).pack(fill="x", pady=(5, 0))
    controls = tk.Frame(header, bg=CARD)
    if compact_header:
        controls.pack(fill="x", pady=(10, 0))
    else:
        controls.pack(side="right", padx=(16, 0))
    status_label = tk.Label(
        controls, text="INITIALIZING", bg=CARD2, fg=ORANGE,
        font=("Consolas", 8, "bold"), padx=12, pady=7,
    )
    status_label.pack(side="left", padx=(0, 7))
    overlay_button = make_action_btn(
        controls, "OPEN OVERLAY", GREEN, TEXT, GREEN_HOVER, TEXT,
        open_hardware_overlay, scale=1.0,
    )
    overlay_button.pack(side="left")
    monitor_ui["header"] = header
    monitor_ui["status"] = status_label
    monitor_ui["overlay_button"] = overlay_button

    if hardware_monitor is None:
        waiting = hardware_monitor_state in {"standby", "starting", "stopping"}
        unavailable = tk.Frame(frame, bg=BG)
        unavailable.grid(row=1, column=0, sticky="ew", pady=90)
        tk.Label(
            unavailable, text="⌁", bg=BG,
            fg=CYAN if waiting else ORANGE, font=("Segoe UI", 46, "bold"),
        ).pack()
        tk.Label(
            unavailable,
            text="STARTING HARDWARE TELEMETRY" if waiting else "HARDWARE TELEMETRY UNAVAILABLE",
            bg=BG, fg=TEXT, font=("Segoe UI Black", 15, "bold"),
        ).pack(pady=(3, 6))
        detail = (
            "Loading monitoring services in the background. The launcher remains responsive."
            if waiting else
            (hardware_monitor_error or "Install psutil and restart XVVIIX to activate the integrated monitor.")
        )
        tk.Label(
            unavailable, text=detail, bg=BG, fg=SUBTEXT,
            font=("Segoe UI", 10), wraplength=620, justify="center",
        ).pack()
        status_label.config(text="STARTING" if waiting else "OFFLINE", fg=CYAN if waiting else RED)
        if waiting and hardware_monitor_state == "standby":
            request_hardware_monitor_start()
        return

    metric_panel = tk.Frame(frame, bg=BG)
    metric_panel.grid(row=1, column=0, sticky="ew", padx=5, pady=0)
    metric_columns = 2 if root.winfo_width() < 980 else 4
    for column in range(metric_columns):
        metric_panel.grid_columnconfigure(column, weight=1, uniform="monitor-metric")
    metric_specs = (
        ("cpu", "CPU LOAD", CYAN), ("gpu", "GPU LOAD", ACCENT2),
        ("memory", "MEMORY", GREEN_HOVER), ("storage", "SYSTEM STORAGE", ORANGE),
    )
    for index, (key, title, accent) in enumerate(metric_specs):
        card = _monitor_metric_card(metric_panel, index % metric_columns, title, accent)
        card["card"].grid_configure(row=index // metric_columns)
        monitor_ui[key] = card

    secondary = tk.Frame(frame, bg=BG)
    secondary.grid(row=2, column=0, sticky="ew", padx=5, pady=0)
    secondary_columns = 1 if root.winfo_width() < 900 else 2
    for column in range(secondary_columns):
        secondary.grid_columnconfigure(column, weight=1, uniform="monitor-secondary")
    for index, (key, title, accent) in enumerate((
        ("network_panel", "NETWORK LINK", NEON),
        ("system_panel", "SYSTEM IDENTITY", ACCENT2),
    )):
        panel = tk.Frame(
            secondary, bg=CARD, padx=16, pady=13,
            highlightbackground=BORDER_SOFT, highlightthickness=1,
        )
        panel.grid(
            row=index // secondary_columns, column=index % secondary_columns,
            sticky="nsew", padx=5, pady=5,
        )
        tk.Label(panel, text=title, bg=CARD, fg=accent, font=("Consolas", 8, "bold")).pack(anchor="w")
        primary = tk.Label(
            panel, text="Waiting for telemetry", bg=CARD, fg=TEXT,
            font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
        )
        primary.pack(fill="x", pady=(7, 2))
        secondary_label = tk.Label(
            panel, text="--", bg=CARD, fg=SUBTEXT,
            font=("Segoe UI", 8), anchor="w", justify="left",
        )
        secondary_label.pack(fill="x")
        tertiary_label = tk.Label(
            panel, text="--", bg=CARD, fg=MUTED,
            font=("Consolas", 7), anchor="w", justify="left",
            wraplength=max(300, (root.winfo_width() - 110) // secondary_columns),
        )
        tertiary_label.pack(fill="x", pady=(4, 0))
        monitor_ui[key] = {
            "primary": primary, "secondary": secondary_label, "tertiary": tertiary_label,
        }

    process_panel = tk.Frame(
        frame, bg=CARD, padx=16, pady=13,
        highlightbackground=BORDER, highlightthickness=1,
    )
    process_panel.grid(row=3, column=0, sticky="nsew", padx=10, pady=(6, 12))
    process_toolbar = tk.Frame(process_panel, bg=CARD)
    process_toolbar.pack(fill="x", pady=(0, 9))
    process_title = tk.Frame(process_toolbar, bg=CARD)
    process_title.pack(side="left", fill="x", expand=True)
    tk.Label(
        process_title, text="EXACT PROCESS TELEMETRY", bg=CARD, fg=TEXT,
        font=("Segoe UI", 11, "bold"), anchor="w",
    ).pack(anchor="w")
    process_count = tk.Label(
        process_title, text="WAITING FOR CURRENT-USER PROCESSES", bg=CARD, fg=NEON,
        font=("Consolas", 7, "bold"), anchor="w",
    )
    process_count.pack(anchor="w", pady=(2, 0))
    monitor_ui["process_count"] = process_count

    filter_tools = tk.Frame(process_toolbar, bg=CARD)
    filter_tools.pack(side="right")
    monitor_process_query_var = tk.StringVar(value=monitor_process_filter)
    process_search = tk.Entry(
        filter_tools, textvariable=monitor_process_query_var, bg=BG2, fg=TEXT,
        insertbackground=NEON, relief="flat", highlightbackground=BORDER,
        highlightthickness=1, width=18, font=("Segoe UI", 9),
    )
    process_search.pack(side="left", ipady=6, padx=(0, 8))
    monitor_process_query_var.trace_add("write", set_monitor_process_filter)
    sort_buttons = {}
    for key, label in (("cpu", "CPU"), ("memory", "RAM"), ("name", "NAME"), ("pid", "PID")):
        selected = key == monitor_process_sort
        button = tk.Button(
            filter_tools, text=label, bg=ACCENT if selected else CARD2,
            fg=TEXT if selected else SUBTEXT, relief="flat", bd=0,
            padx=9, pady=5, cursor="hand2", font=("Segoe UI", 7, "bold"),
            command=lambda selected_key=key: set_monitor_process_sort(selected_key),
        )
        button.pack(side="left", padx=2)
        bind_animated_button(
            button, ACCENT if selected else CARD2, ACCENT2,
            TEXT if selected else SUBTEXT, TEXT,
        )
        sort_buttons[key] = button
    monitor_ui["sort_buttons"] = sort_buttons

    tree_container = tk.Frame(process_panel, bg=CARD2)
    tree_container.pack(fill="both", expand=True)
    style = ttk.Style(root)
    style.configure(
        "XVVIIXMonitor.Treeview", background=CARD2, fieldbackground=CARD2,
        foreground=TEXT, rowheight=25, borderwidth=0, font=("Segoe UI", 8),
    )
    style.configure(
        "XVVIIXMonitor.Treeview.Heading", background=BG2, foreground=NEON,
        relief="flat", font=("Consolas", 8, "bold"),
    )
    style.map(
        "XVVIIXMonitor.Treeview", background=[("selected", ACCENT)],
        foreground=[("selected", TEXT)],
    )
    columns = ("name", "pid", "cpu", "memory", "threads", "status")
    tree = ttk.Treeview(
        tree_container, columns=columns, show="headings", height=11,
        style="XVVIIXMonitor.Treeview", selectmode="browse",
    )
    headings = {
        "name": "PROCESS", "pid": "PID", "cpu": "CPU", "memory": "MEMORY",
        "threads": "THREADS", "status": "STATE",
    }
    widths = {"name": 250, "pid": 70, "cpu": 75, "memory": 105, "threads": 80, "status": 105}
    for column in columns:
        tree.heading(column, text=headings[column])
        tree.column(
            column, width=widths[column], minwidth=55,
            anchor="w" if column in {"name", "status"} else "center",
            stretch=column == "name",
        )
    tree.tag_configure("even", background=CARD2, foreground=TEXT)
    tree.tag_configure("odd", background="#111c31", foreground=TEXT)
    tree.pack(side="left", fill="both", expand=True)
    tree_scrollbar = ttk.Scrollbar(
        tree_container, orient="vertical", command=tree.yview,
        style="XVVIIX.Vertical.TScrollbar",
    )
    tree_scrollbar.pack(side="right", fill="y")
    tree.configure(yscrollcommand=tree_scrollbar.set)
    tree.bind("<<TreeviewSelect>>", _selected_monitor_process)
    tree.bind(
        "<MouseWheel>",
        lambda event: (tree.yview_scroll(int(-event.delta / 120), "units"), "break")[1],
    )
    process_detail = tk.Label(
        process_panel, text="Select a process to inspect its user and executable path.",
        bg=CARD, fg=SUBTEXT, font=("Consolas", 7), anchor="w",
        justify="left", wraplength=max(520, root.winfo_width() - 110),
    )
    process_detail.pack(fill="x", pady=(8, 0))
    monitor_ui["process_tree"] = tree
    monitor_ui["process_detail"] = process_detail
    monitor_ui["process_records"] = {}
    monitor_ui["last_snapshot_timestamp"] = -1.0
    update_hardware_monitor_view(force=True)


def update_hardware_monitor_view(force=False):
    global monitor_refresh_job, monitor_history_timestamp
    monitor_refresh_job = None
    if active_tab != "monitor" or hardware_monitor is None or not monitor_ui:
        return
    try:
        snapshot = hardware_monitor.snapshot()
        timestamp = float(snapshot.get("timestamp", 0.0) or 0.0)
        if not force and timestamp == monitor_ui.get("last_snapshot_timestamp"):
            _schedule_hardware_monitor_view_refresh()
            return
        monitor_ui["last_snapshot_timestamp"] = timestamp
        status = str(snapshot.get("status") or "starting")
        status_label = monitor_ui.get("status")
        if widget_exists(status_label):
            status_label.config(
                text="LIVE" if status == "online" else status.upper(),
                fg=GREEN_HOVER if status == "online" else ORANGE,
            )

        cpu = snapshot.get("cpu", {})
        gpu = snapshot.get("gpu", {})
        memory = snapshot.get("memory", {})
        storage = snapshot.get("storage", {})
        network = snapshot.get("network", {})
        system = snapshot.get("system", {})
        static = snapshot.get("static", {})
        cpu_pct = _monitor_percent(cpu.get("percent")) or 0.0
        gpu_pct = _monitor_percent(gpu.get("usage"))
        memory_pct = _monitor_percent(memory.get("percent")) or 0.0
        storage_pct = _monitor_percent(storage.get("percent")) or 0.0
        if timestamp > monitor_history_timestamp:
            monitor_history["cpu"].append(cpu_pct)
            monitor_history["gpu"].append(gpu_pct or 0.0)
            monitor_history["memory"].append(memory_pct)
            monitor_history["storage"].append(storage_pct)
            monitor_history_timestamp = timestamp

        cpu_card = monitor_ui["cpu"]
        cpu_card["value"].config(text=f"{cpu_pct:.0f}%")
        frequency = cpu.get("current_mhz")
        temperature = cpu.get("temperature")
        cpu_card["detail"].config(text=(
            f"{static.get('physical_cores', '--')}C / {static.get('logical_cores', '--')}T"
            f"   ·   {frequency / 1000:.2f} GHz" if frequency else
            f"{static.get('physical_cores', '--')}C / {static.get('logical_cores', '--')}T   ·   CLOCK --"
        ) + (f"   ·   {temperature:.0f}°C" if temperature is not None else ""))

        gpu_card = monitor_ui["gpu"]
        gpu_card["value"].config(text=f"{gpu_pct:.0f}%" if gpu_pct is not None else "N/A")
        gpu_detail = str(gpu.get("name") or "GPU telemetry unavailable")
        if gpu.get("vram_used") is not None:
            gpu_detail += f"   ·   {_monitor_bytes(gpu.get('vram_used'))} / {_monitor_bytes(gpu.get('vram_total'))}"
        if gpu.get("temperature") is not None:
            gpu_detail += f"   ·   {gpu['temperature']:.0f}°C"
        gpu_extended = []
        if gpu.get("clock_mhz") is not None:
            gpu_extended.append(f"CLOCK {gpu['clock_mhz']:.0f} MHz")
        if gpu.get("power_w") is not None:
            gpu_extended.append(f"POWER {gpu['power_w']:.0f} W")
        if gpu.get("fan_percent") is not None:
            gpu_extended.append(f"FAN {gpu['fan_percent']:.0f}%")
        if gpu_extended:
            gpu_detail += "\n" + "   ·   ".join(gpu_extended)
        gpu_card["detail"].config(text=gpu_detail[:180])

        memory_card = monitor_ui["memory"]
        memory_card["value"].config(text=f"{memory_pct:.0f}%")
        memory_card["detail"].config(
            text=(
                f"{_monitor_bytes(memory.get('used'))} USED   ·   {_monitor_bytes(memory.get('available'))} AVAILABLE"
                f"\nSWAP {_monitor_percent(memory.get('swap_percent')) or 0.0:.0f}%   ·   {_monitor_bytes(memory.get('swap_used'))} / {_monitor_bytes(memory.get('swap_total'))}"
            )
        )
        storage_card = monitor_ui["storage"]
        storage_card["value"].config(text=f"{storage_pct:.0f}%")
        storage_card["detail"].config(
            text=(
                f"{_monitor_bytes(storage.get('free'))} FREE / {_monitor_bytes(storage.get('total'))}"
                f"\nREAD {_monitor_rate(storage.get('read_rate'))}   ·   WRITE {_monitor_rate(storage.get('write_rate'))}"
            )
        )
        for key in ("cpu", "gpu", "memory", "storage"):
            _draw_monitor_history(monitor_ui[key]["chart"], monitor_history[key], monitor_ui[key]["accent"])

        latency = network.get("latency_ms")
        interfaces = network.get("interfaces", [])
        network_panel = monitor_ui["network_panel"]
        network_panel["primary"].config(
            text=f"↓  {_monitor_rate(network.get('download_rate'))}     ↑  {_monitor_rate(network.get('upload_rate'))}     PING  {latency:.0f} ms" if latency is not None
            else f"↓  {_monitor_rate(network.get('download_rate'))}     ↑  {_monitor_rate(network.get('upload_rate'))}     PING  --"
        )
        interface_names = ", ".join(str(item.get("name")) for item in interfaces[:6]) or "No active interface details"
        network_panel["secondary"].config(
            text=f"{len(interfaces)} ACTIVE LINK(S)   ·   {network.get('ipv4_count', 0)} IPv4 ADDRESS(ES)   ·   {interface_names}"
        )
        network_details = []
        for interface in interfaces[:4]:
            speed = f"{interface.get('speed_mbps', 0)} Mbps" if interface.get("speed_mbps") else "speed unavailable"
            addresses = ", ".join(interface.get("ipv4", [])[:2]) or "no IPv4"
            network_details.append(f"{interface.get('name', 'LINK')}: {speed}, {addresses}")
        network_panel["tertiary"].config(
            text="   ·   ".join(network_details) if network_details else "No local interface metadata exposed"
        )

        system_panel = monitor_ui["system_panel"]
        system_panel["primary"].config(
            text=f"{static.get('hostname', 'Unknown')}   ·   {static.get('os', 'Unknown OS')}   ·   UPTIME {_monitor_uptime(system.get('uptime_seconds'))}"
        )
        battery = system.get("battery")
        battery_text = "NO BATTERY"
        if battery and battery.get("percent") is not None:
            battery_text = f"BATTERY {battery['percent']:.0f}% {'AC' if battery.get('plugged') else 'MOBILE'}"
        system_panel["secondary"].config(
            text=f"{system.get('process_count', 0)} TOTAL PROCESSES   ·   {system.get('user_process_count', 0)} USER PROCESSES   ·   {system.get('user_thread_count', 0)} USER THREADS   ·   {battery_text}"
        )
        temperatures = system.get("temperatures", [])
        temperature_text = ", ".join(
            f"{entry.get('sensor', 'SENSOR')} {entry.get('temperature', 0):.0f}°C"
            for entry in temperatures[:4]
        ) or "No thermal sensors exposed"
        system_panel["tertiary"].config(
            text=f"{static.get('cpu_model', 'Unknown CPU')}   ·   {static.get('architecture', 'Unknown architecture')}   ·   {temperature_text}"
        )
        _update_monitor_process_table(
            snapshot.get("processes", []), snapshot.get("user_process_total", 0),
            bool(snapshot.get("processes_truncated")),
        )
    except (tk.TclError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        LOG.debug("Hardware Monitor view refresh skipped: %s", exc)
    if active_tab == "monitor":
        _schedule_hardware_monitor_view_refresh()


def close_hardware_overlay():
    global monitor_overlay, monitor_overlay_job, monitor_overlay_requested
    monitor_overlay_requested = False
    if monitor_overlay_job is not None and root is not None:
        try:
            root.after_cancel(monitor_overlay_job)
        except (tk.TclError, ValueError):
            pass
    monitor_overlay_job = None
    if widget_exists(monitor_overlay):
        try:
            monitor_overlay.destroy()
        except tk.TclError:
            pass
    monitor_overlay = None
    monitor_overlay_ui.clear()
    if active_tab != "monitor":
        schedule_hardware_monitor_idle_stop()


def toggle_hardware_overlay_mode():
    if not widget_exists(monitor_overlay):
        return
    compact = not bool(monitor_overlay_ui.get("compact"))
    monitor_overlay_ui["compact"] = compact
    details = monitor_overlay_ui.get("details")
    if widget_exists(details):
        if compact:
            details.pack_forget()
        else:
            details.pack(fill="x", pady=(8, 0))
    monitor_overlay_ui["toggle"].config(text="EXPAND" if compact else "COMPACT")
    monitor_overlay.update_idletasks()
    width = 470 if compact else 520
    height = 112 if compact else 165
    monitor_overlay.geometry(f"{width}x{height}+{monitor_overlay.winfo_x()}+{monitor_overlay.winfo_y()}")


def open_hardware_overlay():
    global monitor_overlay, monitor_overlay_requested
    cancel_hardware_monitor_idle_stop()
    if hardware_monitor is None:
        if hardware_monitor_state in {"unavailable", "failed"}:
            messagebox.showerror(
                "Hardware Monitor",
                hardware_monitor_error or "Hardware telemetry is unavailable. Install psutil and restart XVVIIX.",
            )
            return
        monitor_overlay_requested = True
        request_hardware_monitor_start()
        status_label = monitor_ui.get("status")
        if widget_exists(status_label):
            status_label.config(text="STARTING", fg=CYAN)
        return
    if widget_exists(monitor_overlay):
        monitor_overlay.deiconify()
        monitor_overlay.lift()
        return
    monitor_overlay = tk.Toplevel(root)
    monitor_overlay.title("XVVIIX Hardware Overlay")
    monitor_overlay.configure(bg=BORDER)
    monitor_overlay.overrideredirect(True)
    monitor_overlay.attributes("-topmost", True)
    monitor_overlay.geometry(f"520x165+{max(10, root.winfo_screenwidth() - 550)}+36")
    shell = tk.Frame(monitor_overlay, bg=CARD, padx=14, pady=11)
    shell.pack(fill="both", expand=True, padx=1, pady=1)
    header = tk.Frame(shell, bg=CARD, cursor="fleur")
    header.pack(fill="x")
    tk.Label(
        header, text="⌁  XVVIIX HARDWARE OVERLAY", bg=CARD, fg=NEON,
        font=("Consolas", 9, "bold"),
    ).pack(side="left")
    close_button = tk.Button(
        header, text="×", command=close_hardware_overlay, bg=CARD,
        fg=SUBTEXT, activebackground=RED, activeforeground=TEXT,
        relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 10, "bold"),
    )
    close_button.pack(side="right")
    toggle = tk.Button(
        header, text="COMPACT", command=toggle_hardware_overlay_mode,
        bg=CARD2, fg=SUBTEXT, relief="flat", bd=0, cursor="hand2",
        padx=8, pady=3, font=("Segoe UI", 7, "bold"),
    )
    toggle.pack(side="right", padx=(0, 7))
    metrics = tk.Label(
        shell, text="CPU --   GPU --   RAM --   DISK --", bg=CARD, fg=TEXT,
        font=("Consolas", 14, "bold"), anchor="w",
    )
    metrics.pack(fill="x", pady=(10, 0))
    details = tk.Frame(shell, bg=CARD)
    details.pack(fill="x", pady=(8, 0))
    rates = tk.Label(details, text="", bg=CARD, fg=SUBTEXT, font=("Consolas", 9), anchor="w")
    rates.pack(fill="x")
    processes = tk.Label(
        details, text="", bg=CARD, fg=SUBTEXT, font=("Segoe UI", 8),
        anchor="w", justify="left", wraplength=485,
    )
    processes.pack(fill="x", pady=(6, 0))
    drag_state = {"x": 0, "y": 0}

    def drag_start(event):
        drag_state["x"] = event.x
        drag_state["y"] = event.y

    def drag_move(event):
        x = monitor_overlay.winfo_pointerx() - drag_state["x"]
        y = monitor_overlay.winfo_pointery() - drag_state["y"]
        monitor_overlay.geometry(f"+{x}+{y}")

    for draggable in (header, *header.winfo_children()[:1]):
        draggable.bind("<ButtonPress-1>", drag_start)
        draggable.bind("<B1-Motion>", drag_move)
    monitor_overlay.protocol("WM_DELETE_WINDOW", close_hardware_overlay)
    monitor_overlay.bind("<Escape>", lambda _event: close_hardware_overlay())
    monitor_overlay_ui.update({
        "compact": False, "toggle": toggle, "metrics": metrics,
        "details": details, "rates": rates, "processes": processes,
    })
    update_hardware_overlay()


def update_hardware_overlay():
    global monitor_overlay_job
    monitor_overlay_job = None
    if not widget_exists(monitor_overlay) or hardware_monitor is None:
        return
    try:
        snapshot = hardware_monitor.snapshot()
        cpu = _monitor_percent(snapshot.get("cpu", {}).get("percent"))
        gpu = _monitor_percent(snapshot.get("gpu", {}).get("usage"))
        memory = _monitor_percent(snapshot.get("memory", {}).get("percent"))
        storage = _monitor_percent(snapshot.get("storage", {}).get("percent"))
        pct = lambda value: f"{value:.0f}%" if value is not None else "N/A"
        monitor_overlay_ui["metrics"].config(
            text=f"CPU {pct(cpu)}   GPU {pct(gpu)}   RAM {pct(memory)}   DISK {pct(storage)}"
        )
        network = snapshot.get("network", {})
        latency = network.get("latency_ms")
        ping = f"{latency:.0f}ms" if latency is not None else "--"
        monitor_overlay_ui["rates"].config(
            text=f"NET  ↓{_monitor_rate(network.get('download_rate'))}  ↑{_monitor_rate(network.get('upload_rate'))}  ·  PING {ping}"
        )
        top = snapshot.get("processes", [])[:3]
        process_text = "   ·   ".join(
            f"{item.get('name', 'Unknown')[:18]} {(_monitor_percent(item.get('cpu_percent')) or 0.0):.1f}%"
            for item in top
        ) or "No current-user process telemetry"
        monitor_overlay_ui["processes"].config(text="TOP USER PROCESSES  //  " + process_text)
    except (tk.TclError, RuntimeError, TypeError, ValueError, KeyError):
        return
    monitor_overlay_job = root.after(900, update_hardware_overlay)


def display_reports():
    with data_lock:
        selected = next((entry for entry in reports if entry.get("id") == selected_report_id), None)
    if selected is not None and selected.get("kind") == "system_report":
        display_system_report_detail(selected)
        return
    clear_item_widgets()
    for column in range(4):
        frame.grid_columnconfigure(column, weight=1 if column == 0 else 0, uniform="")

    with data_lock:
        all_report_items = list(reports)
    system_count = sum(report.get("kind") == "system_report" for report in all_report_items)
    crash_count = len(all_report_items) - system_count
    if report_filter == "system":
        report_items = [report for report in all_report_items if report.get("kind") == "system_report"]
    elif report_filter == "crash":
        report_items = [report for report in all_report_items if report.get("kind") != "system_report"]
    else:
        report_items = all_report_items
    total = len(report_items)
    total_all = len(all_report_items)
    critical = sum(
        report.get("severity") in ("critical", "error") for report in all_report_items
    )

    command = tk.Frame(
        frame, bg=CARD, padx=20, pady=16,
        highlightbackground=BORDER, highlightthickness=1,
    )
    command.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 10))
    compact_reports = root.winfo_width() < 900
    title_column = tk.Frame(command, bg=CARD)
    if compact_reports:
        title_column.pack(fill="x")
    else:
        title_column.pack(side="left", fill="x", expand=True)
    tk.Label(
        title_column, text="△  MISSION REPORTS  //  SYSTEMS ANALYSIS",
        bg=CARD, fg=TEXT,
        font=("Segoe UI Black", 12 if compact_reports else 15, "bold"),
    ).pack(anchor="w")
    tk.Label(
        title_column,
        text="SYS REPORT + CRASH DIAGNOSTICS  ·  ENCRYPTED LOCAL ARCHIVE  ·  NO CLOUD",
        bg=CARD, fg=NEON, font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(4, 0))

    metrics = tk.Frame(command, bg=CARD)
    if compact_reports:
        metrics.pack(fill="x", pady=(10, 0))
    else:
        metrics.pack(side="right")
    run_report_btn = tk.Button(
        metrics, text="△  RUN SYS REPORT", bg=GREEN, fg=TEXT,
        relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        font=("Segoe UI", 8, "bold"), command=start_system_report,
        state="disabled" if sys_report_running else "normal",
        disabledforeground=SUBTEXT,
    )
    run_report_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(run_report_btn, GREEN, GREEN_HOVER, TEXT, TEXT)
    tk.Label(
        metrics, text=f"{total_all:02d}  REPORTS", bg=CARD2, fg=NEON,
        font=("Consolas", 9, "bold"), padx=12, pady=7,
    ).pack(side="left", padx=4)
    tk.Label(
        metrics, text=f"{critical:02d}  CRITICAL", bg=CARD2,
        fg=RED if critical else GREEN, font=("Consolas", 9, "bold"),
        padx=12, pady=7,
    ).pack(side="left", padx=4)
    clear_btn = tk.Button(
        metrics, text="CLEAR ALL", bg=CARD2, fg=MUTED,
        relief="flat", bd=0, padx=12, pady=7, cursor="hand2",
        font=("Segoe UI", 8, "bold"), command=clear_all_reports,
        state="normal" if total_all else "disabled", disabledforeground=BORDER,
    )
    clear_btn.pack(side="left", padx=(8, 0))
    bind_animated_button(clear_btn, CARD2, RED, MUTED, TEXT)

    filters = tk.Frame(frame, bg=BG)
    filters.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
    tk.Label(
        filters, text="PREVIOUS REPORTS", bg=BG, fg=MUTED,
        font=("Consolas", 8, "bold"),
    ).pack(side="left", padx=(2, 12))
    filter_options = (
        ("all", f"ALL  {total_all}"),
        ("system", f"SYS  {system_count}"),
        ("crash", f"CRASH  {crash_count}"),
    )
    for value, label in filter_options:
        selected_filter = report_filter == value
        button = tk.Button(
            filters, text=label,
            bg=TAB_ACT if selected_filter else CARD2,
            fg=TEXT if selected_filter else SUBTEXT,
            relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
            font=("Segoe UI", 8, "bold"),
            command=lambda selected_value=value: set_report_filter(selected_value),
        )
        button.pack(side="left", padx=3)
        bind_animated_button(
            button, TAB_ACT if selected_filter else CARD2,
            ACCENT2 if selected_filter else ACCENT,
            TEXT if selected_filter else SUBTEXT, TEXT,
        )

    if not report_items:
        empty = tk.Frame(frame, bg=BG)
        empty.grid(row=2, column=0, pady=85)
        tk.Label(
            empty, text="◇", bg=BG, fg=GREEN,
            font=("Segoe UI", 46, "bold"),
        ).pack()
        tk.Label(
            empty, text="NO SAVED REPORTS", bg=BG, fg=TEXT,
            font=("Segoe UI Black", 16, "bold"),
        ).pack(pady=(4, 6))
        tk.Label(
            empty,
            text="Run SYS REPORT for a full diagnostic, or launch games to capture crash reports.",
            bg=BG, fg=SUBTEXT, font=("Segoe UI", 10),
        ).pack()
        return

    total_pages = max(1, (total + REPORT_PAGE_SIZE - 1) // REPORT_PAGE_SIZE)
    page = min(page_by_tab["reports"], total_pages - 1)
    page_by_tab["reports"] = page
    page_items = report_items[page * REPORT_PAGE_SIZE:(page + 1) * REPORT_PAGE_SIZE]
    wrap = max(420, root.winfo_width() - 250)

    for index, report in enumerate(page_items, start=2):
        is_system = report.get("kind") == "system_report"
        severity_color = report_severity_color(report.get("severity"))
        card = tk.Frame(
            frame, bg=CARD, padx=18, pady=14,
            highlightbackground=BORDER_SOFT, highlightthickness=1,
        )
        card.grid(row=index, column=0, sticky="ew", padx=10, pady=6)
        tk.Frame(card, bg=severity_color, width=4).pack(side="left", fill="y", padx=(0, 14))
        body = tk.Frame(card, bg=CARD)
        body.pack(side="left", fill="both", expand=True)

        meta = tk.Frame(body, bg=CARD)
        meta.pack(fill="x")
        tk.Label(
            meta,
            text=f"  {report.get('severity', 'warning').upper()}  ",
            bg=severity_color, fg=TEXT,
            font=("Segoe UI", 7, "bold"), pady=2,
        ).pack(side="left")
        kind_label = report.get("kind", "game_crash").replace("_", " ").upper()
        tk.Label(
            meta, text=f"{kind_label}   ·   {format_report_timestamp(report)}",
            bg=CARD, fg=MUTED, font=("Consolas", 8, "bold"),
        ).pack(side="left", padx=10)
        tk.Label(
            meta,
            text=(f"HEALTH {report.get('health_score', 0):02d}/100" if is_system else report.get("exit_hex") or "EXIT UNKNOWN"),
            bg=CARD, fg=severity_color, font=("Consolas", 9, "bold"),
        ).pack(side="right")

        tk.Label(
            body, text=report.get("title") if is_system else report.get("item_name") or report.get("title"),
            bg=CARD, fg=TEXT, font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(9, 2))
        tk.Label(
            body, text=report.get("cause", "Unknown failure"),
            bg=CARD, fg=severity_color, font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            body,
            text=(
                f"SCAN {report.get('scan_duration_ms', 0) / 1000:.1f}S   ·   {len(report.get('sections', [])):02d} SYSTEM MODULES"
                f"   ·   SOURCE {report.get('source', '').upper()}"
                if is_system else
                f"PID {report.get('pid', 0)}   ·   RUNTIME {format_time(report.get('runtime_seconds', 0)).upper()}"
                f"   ·   SOURCE {report.get('source', '').upper()}"
            ),
            bg=CARD, fg=SUBTEXT, font=("Consolas", 8), anchor="w",
        ).pack(fill="x", pady=(5, 8))
        tk.Label(
            body, text=report.get("details", ""),
            bg=CARD2, fg="#c4d1e5", font=("Segoe UI", 9),
            justify="left", anchor="w", wraplength=wrap, padx=10, pady=8,
        ).pack(fill="x")

        suggestions = report.get("suggestions", [])
        if suggestions:
            tk.Label(
                body,
                text=("HEALTH ACTIONS  //  " if is_system else "NEXT ACTIONS  //  ") + "   •   ".join(suggestions[:3]),
                bg=CARD, fg=SUBTEXT, font=("Segoe UI", 8, "bold"),
                justify="left", anchor="w", wraplength=wrap,
            ).pack(fill="x", pady=(8, 2))

        actions = tk.Frame(card, bg=CARD)
        actions.pack(side="right", fill="y", padx=(14, 0))
        if is_system:
            view_btn = make_action_btn(
                actions, "VIEW FULL", GREEN, TEXT, GREEN_HOVER, TEXT,
                lambda entry=report: view_report(entry), scale=1.0,
            )
            view_btn.pack(fill="x", pady=(0, 5))
        copy_btn = make_action_btn(
            actions, "COPY", CARD2, SUBTEXT, ACCENT, TEXT,
            lambda entry=report: copy_report(entry), scale=1.0,
        )
        copy_btn.pack(fill="x", pady=(0, 5))
        delete_btn = make_action_btn(
            actions, "DELETE", CARD2, MUTED, RED, TEXT,
            lambda entry=report: delete_report(entry), scale=1.0,
        )
        delete_btn.pack(fill="x")

    if total_pages > 1:
        navigation = tk.Frame(frame, bg=BG, pady=12)
        navigation.grid(row=len(page_items) + 2, column=0, sticky="ew")
        previous = tk.Button(
            navigation, text="←  PREVIOUS", command=lambda: change_page(-1),
            state="normal" if page > 0 else "disabled",
            bg=CARD2, fg=TEXT, disabledforeground=MUTED,
            relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=7,
        )
        previous.pack(side="left", padx=10)
        tk.Label(
            navigation, text=f"REPORT PAGE {page + 1:02d} / {total_pages:02d}",
            bg=BG, fg=SUBTEXT, font=("Consolas", 9, "bold"),
        ).pack(side="left", expand=True)
        following = tk.Button(
            navigation, text="NEXT  →", command=lambda: change_page(1),
            state="normal" if page + 1 < total_pages else "disabled",
            bg=CARD2, fg=TEXT, disabledforeground=MUTED,
            relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=7,
        )
        following.pack(side="right", padx=10)


def display_items(item_list):
    clear_item_widgets()
    width = root.winfo_width()
    columns = 1 if width < 600 else (2 if width < 1000 else (3 if width < 1400 else 4))
    for column in range(4):
        frame.grid_columnconfigure(
            column, weight=1 if column < columns else 0,
            uniform="group1" if column < columns else "",
        )

    if not item_list:
        ef = tk.Frame(frame, bg=BG)
        ef.grid(row=0, column=0, columnspan=columns, pady=100)
        tk.Label(ef, text="✦", bg=BG, fg=NEON, font=("Segoe UI", 46)).pack()
        tk.Label(ef, text="NO SIGNALS YET", bg=BG, fg=TEXT, font=("Segoe UI Black", 16, "bold")).pack(pady=(4, 6))
        tk.Label(ef, text="Drop a launcher here or create a new entry above", bg=BG, fg=SUBTEXT, font=("Segoe UI", 10)).pack()
        tk.Frame(ef, bg=ACCENT, width=90, height=2).pack(pady=14)
        page_by_tab[active_tab] = 0
        return

    total_items = len(item_list)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page_by_tab[active_tab], total_pages - 1)
    page_by_tab[active_tab] = page
    start = page * PAGE_SIZE
    page_items = item_list[start:start + PAGE_SIZE]

    icon_size = int(48 * current_scale)
    s = current_scale
    base_pad = int(10 * s)

    for i, item in enumerate(page_items):
        color = item["color"]
        color_lite = lerp_color(color, "#ffffff", 0.30)
        card_margin = int(8 * s)
        card = tk.Frame(
            frame, bg=CARD, padx=base_pad, pady=base_pad,
            highlightbackground=BORDER_SOFT, highlightthickness=1,
        )
        card.grid(
            row=i // columns, column=i % columns,
            padx=card_margin, pady=card_margin, sticky="nsew",
        )
        surface_widgets = []

        stripe = tk.Frame(card, bg=color, height=3)
        stripe.pack(fill="x", pady=(0, 10))

        if active_tab == "games":
            category_text, category_color = "GAME", ACCENT
        elif active_tab == "apps":
            category_text, category_color = "APP", CYAN
        else:
            category_text, category_color = "DISCOVERED", ORANGE
        extension = ntpath.splitext(clean_path(item.get("path", "")))[1].replace(".", "").upper() or "FILE"

        content_width = canvas.winfo_width()
        if content_width < 300:
            content_width = max(300, width - 60)
        estimated_card_width = int(content_width / columns) - (card_margin * 2)
        art_width = max(220, estimated_card_width - (base_pad * 2))
        art_height = max(104, int(108 * s))
        backdrop = None
        if active_tab in ("games", "apps"):
            backdrop = get_card_backdrop(item, art_width, art_height)

        if backdrop is not None:
            identity_panel = tk.Canvas(
                card, width=art_width, height=art_height, bg=CARD,
                bd=0, highlightthickness=0,
            )
            identity_panel.pack(fill="x", pady=(0, 10))
            identity_panel.create_image(0, 0, image=backdrop, anchor="nw")
            identity_panel.backdrop_image = backdrop
            surface_widgets.append(identity_panel)

            tag_width = max(49, int((len(category_text) * 7 + 19) * s))
            identity_panel.create_rectangle(
                10, 8, 10 + tag_width, 29,
                fill=category_color, outline="",
            )
            identity_panel.create_text(
                10 + tag_width / 2, 18,
                text=category_text, fill=TEXT,
                font=("Segoe UI", max(7, int(7 * s)), "bold"),
            )
            identity_panel.create_text(
                art_width - 11, 18,
                text="◆  PINNED" if item["pinned"] else extension,
                anchor="e", fill=ACCENT2 if item["pinned"] else MUTED,
                font=(
                    "Segoe UI" if item["pinned"] else "Consolas",
                    max(7, int(7 * s)), "bold",
                ),
            )

            icon = get_icon(item["icon"], icon_size)
            icon_center_y = int(69 * s)
            text_x = max(70, int(71 * s))
            if icon:
                identity_panel.create_image(
                    12, icon_center_y, image=icon, anchor="w",
                )
                identity_panel.foreground_icon = icon
            else:
                letter = item["name"][0].upper() if item["name"] else "?"
                identity_panel.create_text(
                    max(31, int(34 * s)), icon_center_y,
                    text=letter, fill=color_lite,
                    font=("Segoe UI Black", max(22, int(24 * s)), "bold"),
                )

            max_name_chars = max(20, int((art_width - text_x - 12) / max(5.5, 6.6 * s)) * 2)
            display_name = item["name"]
            if len(display_name) > max_name_chars:
                display_name = display_name[:max_name_chars - 1].rstrip() + "…"
            identity_panel.create_text(
                text_x, int(49 * s), text=display_name,
                fill=TEXT, anchor="nw", justify="left",
                width=max(100, art_width - text_x - 12),
                font=("Segoe UI", max(10, int(12 * s)), "bold"),
            )
            identity_panel.create_text(
                text_x, art_height - max(14, int(15 * s)),
                text=compact_display_path(item.get("path", "")),
                fill="#9aabc4", anchor="w",
                font=("Consolas", max(7, int(7 * s))),
            )
        else:
            meta_row = tk.Frame(card, bg=CARD)
            meta_row.pack(fill="x", pady=(0, 9))
            surface_widgets.append(meta_row)
            tk.Label(
                meta_row, text=f"  {category_text}  ", bg=category_color, fg=TEXT,
                font=("Segoe UI", max(7, int(7 * s)), "bold"), pady=2,
            ).pack(side="left")
            if item["pinned"]:
                meta_right = tk.Label(
                    meta_row, text="◆  PINNED", bg=CARD, fg=ACCENT2,
                    font=("Segoe UI", max(7, int(7 * s)), "bold"),
                )
            else:
                meta_right = tk.Label(
                    meta_row, text=extension, bg=CARD, fg=MUTED,
                    font=("Consolas", max(7, int(7 * s)), "bold"),
                )
            meta_right.pack(side="right")
            surface_widgets.append(meta_right)

            hero_row = tk.Frame(card, bg=CARD)
            hero_row.pack(fill="x", pady=(0, 10))
            surface_widgets.append(hero_row)
            icon = get_icon(item["icon"], icon_size)
            if icon:
                icon_label = tk.Label(
                    hero_row, image=icon, bg=CARD,
                    bd=0, highlightthickness=0, padx=0, pady=0,
                )
                icon_label.image = icon
                icon_label.pack(side="left", padx=(0, 11))
            else:
                letter = item["name"][0].upper() if item["name"] else "?"
                icon_label = tk.Label(
                    hero_row, text=letter, bg=CARD, fg=color_lite,
                    font=("Segoe UI Black", max(22, int(24 * s)), "bold"),
                    width=2, height=2, bd=0, highlightthickness=0,
                )
                icon_label.pack(side="left", padx=(0, 11))
            surface_widgets.append(icon_label)

            title_column = tk.Frame(hero_row, bg=CARD)
            title_column.pack(side="left", fill="both", expand=True)
            surface_widgets.append(title_column)
            name_label = tk.Label(
                title_column, text=item["name"], bg=CARD, fg=TEXT,
                font=("Segoe UI", max(10, int(12 * s)), "bold"),
                wraplength=int(210 * s), justify="left", anchor="w",
            )
            name_label.pack(fill="x", pady=(3, 4))
            surface_widgets.append(name_label)
            path_label = tk.Label(
                title_column,
                text=compact_display_path(item.get("path", "")),
                bg=CARD, fg=MUTED,
                font=("Consolas", max(7, int(7 * s))),
                anchor="w", justify="left",
            )
            path_label.pack(fill="x")
            surface_widgets.append(path_label)

        item_sessions = running_sessions_for_item(item) if active_tab in ("games", "apps") else []
        item_running = bool(item_sessions)
        item_ending = any(session.get("end_requested") for _pid, session in item_sessions)
        if item_ending:
            status_text, status_color = "◌  ENDING", ORANGE
        elif item_running:
            status_text, status_color = "●  RUNNING", CYAN
        else:
            status_text, status_color = "●  READY", GREEN
        info_row = tk.Frame(card, bg=CARD2, padx=8, pady=5)
        info_row.pack(fill="x", pady=(0, 9))
        tk.Label(
            info_row, text=status_text, bg=CARD2, fg=status_color,
            font=("Segoe UI", max(7, int(7 * s)), "bold"),
        ).pack(side="left")
        tk.Label(
            info_row, text="◷  " + format_time(item["playtime"]),
            bg=CARD2, fg=SUBTEXT,
            font=("Segoe UI", max(7, int(8 * s)), "bold"),
        ).pack(side="right")

        if active_tab == "games":
            play_text = "▶  PLAY NOW"
        elif active_tab == "apps":
            play_text = "↗  OPEN APP"
        else:
            play_text = "▶  LAUNCH"
        play_btn = tk.Button(
            card, text=play_text, bg=color, fg=TEXT, relief="flat",
            font=("Segoe UI", max(9, int(10 * s)), "bold"),
            pady=max(7, int(7 * s)), cursor="hand2", bd=0,
            activebackground=color_lite, activeforeground=TEXT,
            command=lambda entry=item: run_only(entry),
        )
        play_btn.pack(fill="x", pady=(0, 5))
        bind_animated_button(play_btn, color, color_lite, TEXT, TEXT)

        if active_tab == "games" and item.get("trainer"):
            trainer_btn = tk.Button(
                card, text="⚡  LAUNCH WITH TRAINER", bg=ORANGE, fg=TEXT,
                relief="flat", font=("Segoe UI", max(8, int(8 * s)), "bold"),
                pady=max(5, int(5 * s)), cursor="hand2", bd=0,
                command=lambda entry=item: run_with_trainer(entry),
            )
            trainer_btn.pack(fill="x", pady=(0, 4))
            bind_animated_button(trainer_btn, ORANGE, ORANGE_HOVER, TEXT, TEXT)

        tk.Frame(card, bg=BORDER_SOFT, height=1).pack(fill="x", pady=(7, 7))

        management_row = tk.Frame(card, bg=CARD)
        management_row.pack(fill="x", pady=(0, 5))
        make_action_btn(
            management_row, "↻  CHANGE LOCATION", CARD2, SUBTEXT, ACCENT, TEXT,
            lambda entry=item: change_item_location(entry), scale=s,
        ).pack(side="left", expand=True, fill="x", padx=(0, 3))
        icon_action_btn = make_action_btn(
            management_row, "＋  ADD ICON", CARD2, SUBTEXT, CYAN, TEXT,
            lambda entry=item: add_custom_icon(entry), scale=s,
        )
        if not HAS_PIL:
            icon_action_btn.config(state="disabled", disabledforeground=MUTED)
        icon_action_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
        surface_widgets.append(management_row)

        if active_tab == "founded":
            action_row = make_founded_action_btn(card, item)
        else:
            action_row = tk.Frame(card, bg=CARD)
            action_row.pack(fill="x")
            pin_text = "◆  UNPIN" if item["pinned"] else "◇  PIN TO TOP"
            make_action_btn(
                action_row, pin_text, CARD2, SUBTEXT, ACCENT, TEXT,
                lambda entry=item: toggle_pin(entry), scale=s,
            ).pack(side="left", expand=True, fill="x", padx=(0, 3))
            end_bg = RED if item_running and not item_ending else CARD2
            end_fg = TEXT if item_running and not item_ending else MUTED
            end_btn = make_action_btn(
                action_row, "■  END TASK", end_bg, end_fg, RED, TEXT,
                lambda entry=item: end_task(entry), scale=s,
            )
            end_btn.config(
                state="normal" if item_running and not item_ending else "disabled",
                disabledforeground=MUTED,
            )
            end_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
        surface_widgets.append(action_row)
        bind_context_tree(card, item)
        bind_card_animation(card, stripe, color, surface_widgets, card_margin)
        animate_card_entrance(card, stripe, color, min(i * 14, 180))

    if total_pages > 1:
        nav_row = (len(page_items) + columns - 1) // columns
        navigation = tk.Frame(frame, bg=BG, pady=12)
        navigation.grid(row=nav_row, column=0, columnspan=columns, sticky="ew")
        previous = tk.Button(
            navigation, text="←  PREVIOUS", command=lambda: change_page(-1),
            state="normal" if page > 0 else "disabled",
            bg=CARD2, fg=TEXT, disabledforeground=SUBTEXT,
            relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=7,
        )
        bind_animated_button(previous, CARD2, ACCENT, TEXT, TEXT)
        previous.pack(side="left", padx=8)
        tk.Label(
            navigation,
            text=f"{page + 1:02d}  /  {total_pages:02d}     ·     {total_items} ITEMS",
            bg=BG, fg=SUBTEXT, font=("Segoe UI", 9, "bold"),
        ).pack(side="left", expand=True)
        following = tk.Button(
            navigation, text="NEXT  →", command=lambda: change_page(1),
            state="normal" if page + 1 < total_pages else "disabled",
            bg=CARD2, fg=TEXT, disabledforeground=SUBTEXT,
            relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=7,
        )
        bind_animated_button(following, CARD2, ACCENT, TEXT, TEXT)
        following.pack(side="right", padx=8)

# ==========================================
# WINDOWS EFFECTS
# ==========================================
def enable_win11_round_corners(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(DWMWCP_ROUND)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except (AttributeError, OSError, tk.TclError) as exc:
        LOG.debug("Rounded corners unavailable: %s", exc)
        return False


def enable_glass_effect(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())

        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId", ctypes.c_int),
            ]

        class DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENTPOLICY(4, 0, 0, 0)
        data = DATA(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        result = ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        return bool(result)
    except (AttributeError, OSError, TypeError, tk.TclError) as exc:
        LOG.debug("Glass effect unavailable: %s", exc)
        return False


def enable_window_shadow(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        class_style = ctypes.windll.user32.GetClassLongW(hwnd, -26)
        ctypes.windll.user32.SetClassLongW(hwnd, -26, class_style | 0x00020000)
        return True
    except (AttributeError, OSError, tk.TclError) as exc:
        LOG.debug("Window shadow unavailable: %s", exc)
        return False

# ==========================================
# SPLASH SCREEN
# ==========================================
def show_splash(window, video_path="intro.mp4", width=600, height=350, duration=900):
    """Show a safe Tk-only splash; native video codecs never gate startup."""
    del video_path  # The MP4 is retained as an asset but is not decoded at startup.
    try:
        window.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    window.withdraw()

    splash = tk.Toplevel(window)
    splash.overrideredirect(True)
    splash.configure(bg=BG)
    try:
        splash.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    x = (splash.winfo_screenwidth() - width) // 2
    y = (splash.winfo_screenheight() - height) // 2
    splash.geometry(f"{width}x{height}+{x}+{y}")

    panel = tk.Canvas(splash, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
    panel.pack(fill="both", expand=True)
    panel.create_rectangle(0, 0, width, height, fill=BG, outline="")
    panel.create_rectangle(0, 0, 8, height, fill=ACCENT, outline="")
    panel.create_line(36, 64, width - 36, 64, fill=BORDER, width=1)
    panel.create_line(36, height - 62, width - 36, height - 62, fill=BORDER, width=1)
    panel.create_text(
        width // 2, 126, text="XVVIIX", fill=TEXT,
        font=("Segoe UI Black", 44, "bold"),
    )
    panel.create_text(
        width // 2, 177, text="XVVIIX  //  COMMAND CENTER", fill=NEON,
        font=("Segoe UI", 12, "bold"),
    )
    panel.create_text(
        width // 2, 213, text="INITIALIZING LAUNCH SYSTEMS", fill=SUBTEXT,
        font=("Consolas", 10, "bold"),
    )
    panel.create_rectangle(130, 248, width - 130, 254, fill=BORDER_SOFT, outline="")
    panel.create_rectangle(130, 248, width - 210, 254, fill=ACCENT, outline="")
    panel.create_text(
        width // 2, height - 32, text="LAUNCH  •  TRACK  •  DISCOVER", fill=MUTED,
        font=("Segoe UI", 8, "bold"),
    )

    state = {"closed": False}

    def fade_in_splash(alpha=0.0):
        if state["closed"] or not splash.winfo_exists():
            return
        try:
            splash.attributes("-alpha", min(alpha, 1.0))
        except tk.TclError:
            return
        if alpha < 1.0:
            splash.after(20, fade_in_splash, alpha + 0.08)

    def fade_in_root(alpha=0.0):
        window.deiconify()
        try:
            window.attributes("-alpha", min(alpha, 1.0))
        except tk.TclError:
            return
        if alpha < 1.0:
            window.after(20, fade_in_root, alpha + 0.08)

    def close_splash():
        if state["closed"]:
            return
        state["closed"] = True
        try:
            splash.destroy()
        except tk.TclError:
            pass
        fade_in_root()

    splash.protocol("WM_DELETE_WINDOW", close_splash)
    splash.bind("<Button-1>", lambda _event: close_splash())
    splash.bind("<Escape>", lambda _event: close_splash())
    fade_in_splash()
    splash.after(duration, close_splash)

# ==========================================
# MAIN WINDOW CREATION
# ==========================================
def main():
    global root, canvas, frame, tab_bar, search_entry, name_entry, add_btn, sort_btn
    global stats_lbl, games_tab_btn, apps_tab_btn, founded_tab_btn, reports_tab_btn, monitor_tab_btn, search_var
    global scan_btn, bg_btn, clock_lbl, music_btn, is_fullscreen, hardware_monitor
    global hardware_monitor_state, hardware_monitor_error
    global drop_zone, drop_container, topbar, container, DND_ACTIVE
    global header_canvas, header_image_ref, header_stats_id, activity_rail
    global activity_primary_lbl, activity_secondary_lbl
    global background_music, launcher_settings
    startup_checkpoint("APPLICATION_ENTRY", "STARTED", f"platform={sys.platform}; frozen={bool(getattr(sys, 'frozen', False))}")
    print("Initializing launcher window...", flush=True)
    dnd_enabled = HAS_DND
    try:
        if dnd_enabled:
            try:
                root = TkinterDnD.Tk()
            except Exception as exc:
                LOG.warning("Drag-and-drop initialization failed; using plain Tk: %s", exc)
                dnd_enabled = False
                root = tk.Tk()
        else:
            root = tk.Tk()
    except tk.TclError as exc:
        startup_checkpoint("WINDOW_BACKEND", "FAILED", f"Tk window creation failed: {exc}")
        report_startup_error(f"Could not create the application window:\n{exc}")
        return 1

    DND_ACTIVE = dnd_enabled
    root.withdraw()
    startup_checkpoint(
        "WINDOW_BACKEND",
        "READY" if dnd_enabled else "DEGRADED",
        f"backend={'tkinterdnd2' if dnd_enabled else 'tkinter'}; root=created; initial_state=withdrawn",
    )
    if not dnd_enabled:
        LOG.warning("tkinterdnd2 is unavailable; drag and drop is disabled.")
    if not HAS_PIL:
        LOG.warning("Pillow is unavailable; custom icons and video frames are disabled.")
    if not HAS_PSUTIL:
        LOG.warning("psutil is unavailable; playtime monitoring is disabled.")
    runtime_degraded = [
        name for name, available in (
            ("drag_drop", dnd_enabled), ("pillow", HAS_PIL),
            ("psutil", HAS_PSUTIL), ("hardware_monitor", HAS_HARDWARE_MONITOR),
            ("cryptography", HAS_CRYPTOGRAPHY),
        )
        if not available
    ]
    startup_checkpoint(
        "RUNTIME_CAPABILITIES",
        "DEGRADED" if runtime_degraded else "READY",
        "unavailable=" + (",".join(runtime_degraded) if runtime_degraded else "none"),
    )

    launcher_settings = load_launcher_settings()
    startup_checkpoint(
        "SETTINGS",
        settings_load_status,
        f"{settings_load_detail}; music_enabled={launcher_settings['music_enabled']}; card_art={launcher_settings['card_art_enabled']}",
    )
    background_music = BackgroundMusic(
        MUSIC_FILE,
        enabled=launcher_settings["music_enabled"],
        volume=launcher_settings["music_volume"],
    )
    if not background_music.available:
        LOG.warning("Background music is unavailable; install pygame and verify the music asset.")
    startup_checkpoint(
        "AUDIO_CONTROLLER",
        "READY" if background_music.available else "DEGRADED",
        f"pygame={'deferred' if not _pygame_import_attempted else ('ready' if HAS_PYGAME else 'unavailable')}; asset={os.path.isfile(MUSIC_FILE)}; enabled={background_music.enabled}",
    )

    try:
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except (OSError, tk.TclError) as exc:
        LOG.debug("Window icon unavailable: %s", exc)

    root.configure(bg="#000000")
    if not initialize_data_vault(root):
        startup_checkpoint("DATA_VAULT_AND_LIBRARIES", "ABORTED", "vault setup or unlock did not complete")
        clear_vault_key()
        try:
            root.destroy()
        except tk.TclError:
            pass
        return 0
    root.update_idletasks()
    startup_checkpoint(
        "DATA_VAULT_AND_LIBRARIES",
        "READY",
        f"encrypted=true; games={len(games)}; apps={len(apps)}; discovered={len(founded)}; reports={len(reports)}; activity={len(recent_activity)}; recovery_events={len(load_warnings)}",
    )

    glass_ready = enable_glass_effect(root)
    corners_ready = enable_win11_round_corners(root)
    shadow_ready = enable_window_shadow(root)

    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.after(100, enable_glass_effect, root)
    native_effects = {
        "glass": glass_ready, "rounded_corners": corners_ready, "shadow": shadow_ready,
    }
    startup_checkpoint(
        "NATIVE_WINDOW_EFFECTS",
        "SKIPPED" if os.name != "nt" else ("READY" if all(native_effects.values()) else "DEGRADED"),
        "Windows-only effects not required on this platform" if os.name != "nt" else "; ".join(
            f"{name}={'ready' if available else 'unavailable'}" for name, available in native_effects.items()
        ),
    )

    splash_status = "READY"
    splash_detail = "intro window scheduled"
    splash_duration = 350
    try:
        video_intro_path = resource_path("intro.mp4")
        show_splash(
            root, video_path=video_intro_path,
            width=600, height=350, duration=splash_duration,
        )
    except (OSError, RuntimeError, tk.TclError) as exc:
        splash_status = "DEGRADED"
        splash_detail = f"intro disabled; main window fallback active: {exc}"
        LOG.warning("Splash screen disabled after an error: %s", exc)
        root.deiconify()
        try:
            root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
    startup_checkpoint("SPLASH_PRESENTATION", splash_status, splash_detail)

    root.title("XVVIIX Launcher")
    root.geometry("1180x820")
    root.minsize(760, 640)

    if sys.platform == "win32":
        try:
            root.wm_attributes('-titlebarcolor', TOP_BG)
        except Exception:
            pass

    is_fullscreen = False

    def toggle_fullscreen(event=None):
        global is_fullscreen
        is_fullscreen = not is_fullscreen
        root.attributes("-fullscreen", is_fullscreen)

    def exit_fullscreen(event=None):
        global is_fullscreen
        is_fullscreen = False
        root.attributes("-fullscreen", False)

    root.bind("<F11>", toggle_fullscreen)
    root.bind("<Escape>", exit_fullscreen)
    root.bind_all(
        "<Control-Alt-m>",
        lambda _event: toggle_hardware_overlay_mode()
        if widget_exists(monitor_overlay) else open_hardware_overlay(),
    )
    startup_checkpoint(
        "WINDOW_CONFIGURATION",
        "READY",
        "title=XVVIIX Launcher; geometry=1180x820; minimum=760x640; fullscreen and monitor-overlay bindings=ready",
    )

    # ==========================================
    # UI CONSTRUCTION
    # ==========================================
    header_canvas = tk.Canvas(root, height=156, bg=TOP_BG, highlightthickness=0, bd=0)
    header_canvas.pack(fill="x")

    header_image_id = None
    if HAS_PIL:
        try:
            header_image = Image.open(resource_path("assets/xvviix_header.png")).convert("RGB")
            target_width, crop_height = 1920, 190
            if header_image.size != (target_width, crop_height):
                target_height = int(header_image.height * target_width / header_image.width)
                header_image = header_image.resize((target_width, target_height), Image.LANCZOS)
                crop_top = max(0, (target_height - crop_height) // 2)
                header_image = header_image.crop((0, crop_top, target_width, crop_top + crop_height))
            header_image_ref = ImageTk.PhotoImage(header_image)
            header_image_id = header_canvas.create_image(0, 78, image=header_image_ref, anchor="center")
        except (OSError, ValueError) as exc:
            LOG.warning("Header artwork unavailable: %s", exc)

    header_canvas.create_rectangle(0, 0, 9, 156, fill=ACCENT, outline="")
    header_canvas.create_text(34, 31, text="✦  XVVIIX", anchor="w", fill=TEXT, font=("Segoe UI Black", 27, "bold"))
    header_canvas.create_text(36, 66, text="XVVIIX  //  COMMAND CENTER", anchor="w", fill=NEON, font=("Segoe UI", 10, "bold"))
    header_canvas.create_text(36, 90, text="LAUNCH  •  TRACK  •  DISCOVER", anchor="w", fill=SUBTEXT, font=("Segoe UI", 9, "bold"))
    header_stats_id = header_canvas.create_text(36, 121, text="", anchor="w", fill="#c4b5fd", font=("Segoe UI", 9, "bold"))

    activity_rail = tk.Frame(
        header_canvas, bg="#0b1528",
        highlightbackground=BORDER, highlightthickness=1,
        cursor="hand2",
    )
    rail_heading = tk.Frame(activity_rail, bg="#0b1528")
    rail_heading.pack(fill="x", padx=11, pady=(7, 2))
    tk.Label(
        rail_heading, text="RECENT ACTIVITY  //  LIVE", bg="#0b1528", fg=NEON,
        font=("Consolas", 7, "bold"),
    ).pack(side="left")
    tk.Label(
        rail_heading, text="OPEN REPORTS  ›", bg="#0b1528", fg=MUTED,
        font=("Segoe UI", 7, "bold"),
    ).pack(side="right")
    activity_primary_lbl = tk.Label(
        activity_rail, text="", bg="#0b1528", fg=TEXT,
        font=("Segoe UI", 8, "bold"), anchor="w",
    )
    activity_primary_lbl.pack(fill="x", padx=11, pady=(2, 1))
    activity_secondary_lbl = tk.Label(
        activity_rail, text="", bg="#0b1528", fg=SUBTEXT,
        font=("Segoe UI", 7), anchor="w",
    )
    activity_secondary_lbl.pack(fill="x", padx=11, pady=(0, 6))
    for rail_widget in (
        activity_rail, rail_heading, *rail_heading.winfo_children(),
        activity_primary_lbl, activity_secondary_lbl,
    ):
        rail_widget.bind("<Button-1>", lambda _event: switch_tab("reports"), add="+")

    clock_lbl = tk.Label(
        header_canvas, text="", bg="#0b1528", fg=NEON,
        font=("Consolas", 12, "bold"), padx=13, pady=7,
        highlightbackground=BORDER, highlightthickness=1,
    )
    music_btn = tk.Button(
        header_canvas, text="♫  GALACTIC  ON", bg="#10243a", fg=NEON,
        relief="flat", font=("Segoe UI", 9, "bold"), padx=13, pady=7,
        cursor="hand2", bd=0, command=toggle_background_music,
        activebackground="#164e63", activeforeground=TEXT,
    )
    bg_btn = tk.Button(
        header_canvas, text="◈  THEME", bg=BG2, fg=SUBTEXT,
        relief="flat", font=("Segoe UI", 9, "bold"), padx=14, pady=9,
        cursor="hand2", bd=0, command=show_theme_menu,
    )
    bind_animated_button(music_btn, "#10243a", "#164e63", NEON, TEXT)
    bind_animated_button(bg_btn, BG2, CARD2, SUBTEXT, TEXT)
    music_btn.bind("<Button-3>", show_music_credit, add="+")
    activity_window = header_canvas.create_window(
        0, 0, window=activity_rail, anchor="nw", width=420, height=80,
    )
    clock_window = header_canvas.create_window(0, 0, window=clock_lbl)
    music_window = header_canvas.create_window(0, 0, window=music_btn)
    music_credit_id = header_canvas.create_text(
        0, 0, text="GALACTIC ODYSSEY  •  MUSIC BY ALKAKRAB",
        anchor="center", fill=MUTED, font=("Segoe UI", 7, "bold"),
    )
    theme_window = header_canvas.create_window(0, 0, window=bg_btn)
    update_music_button()

    def layout_header(event=None):
        width = event.width if event is not None else header_canvas.winfo_width()
        if header_image_id is not None:
            header_canvas.coords(header_image_id, width // 2, 78)
        music_x = max(410, width - 230)
        if width >= 900:
            rail_left = 315 if width < 1050 else 335
            rail_right = music_x - 100
            rail_width = max(235, rail_right - rail_left)
            header_canvas.itemconfigure(activity_window, state="normal", width=rail_width)
            header_canvas.coords(activity_window, rail_left, 13)
        else:
            header_canvas.itemconfigure(activity_window, state="hidden")
        header_canvas.coords(clock_window, max(510, width - 88), 33)
        header_canvas.coords(music_window, music_x, 31)
        header_canvas.coords(music_credit_id, music_x, 61)
        header_canvas.coords(theme_window, max(650, width - 66), 107)

    header_canvas.bind("<Configure>", layout_header)

    def update_clock():
        clock_lbl.config(text=time.strftime("%H:%M:%S"))
        root.after(1000, update_clock)
    update_clock()

    accent_line = tk.Frame(root, bg=ACCENT, height=3)
    accent_line.pack(fill="x")
    pulse_widget(accent_line, ACCENT, ACCENT2, cycles=2, duration=220)
    startup_checkpoint(
        "HEADER_INTERFACE",
        "READY" if header_image_id is not None or not HAS_PIL else "DEGRADED",
        f"identity=ready; activity_rail=ready; clock=ready; artwork={'ready' if header_image_id is not None else 'fallback'}",
    )

    tab_bar = tk.Frame(root, bg=BG, pady=12)
    tab_bar.pack(fill="x", padx=30)
    tk.Label(
        tab_bar, text="LIBRARY", bg=BG, fg=MUTED,
        font=("Segoe UI", 9, "bold"), padx=4,
    ).pack(side="left", padx=(0, 14))

    games_tab_btn = tk.Button(tab_bar, text="◉  GAMES", bg=TAB_ACT, fg=TEXT, relief="flat", font=("Segoe UI", 10, "bold"), padx=22, pady=9, cursor="hand2", bd=0, command=lambda: switch_tab("games"))
    games_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(games_tab_btn, TAB_ACT, ACCENT2, TEXT, TEXT)

    apps_tab_btn = tk.Button(tab_bar, text="◇  WORKSPACE", bg=TAB_IN, fg=SUBTEXT, relief="flat", font=("Segoe UI", 10, "bold"), padx=22, pady=9, cursor="hand2", bd=0, command=lambda: switch_tab("apps"))
    apps_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(apps_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    founded_tab_btn = tk.Button(tab_bar, text="✦  DISCOVERED", bg=TAB_IN, fg=SUBTEXT, relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=9, cursor="hand2", bd=0, command=lambda: switch_tab("founded"))
    founded_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(founded_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    reports_tab_btn = tk.Button(tab_bar, text="△  REPORTS", bg=TAB_IN, fg=SUBTEXT, relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=9, cursor="hand2", bd=0, command=lambda: switch_tab("reports"))
    reports_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(reports_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    monitor_tab_btn = tk.Button(tab_bar, text="⌁  MONITOR", bg=TAB_IN, fg=SUBTEXT, relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=9, cursor="hand2", bd=0, command=lambda: switch_tab("monitor"))
    monitor_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(monitor_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)
    startup_checkpoint(
        "NAVIGATION_INTERFACE",
        "READY",
        "tabs=games,workspace,discovered,reports,monitor; active=games",
    )

    drop_container = tk.Frame(root, bg=BG, padx=30)
    drop_container.pack(fill="x", pady=(0, 10))
    drop_zone = tk.Label(
        drop_container,
        text=default_drop_text(),
        bg=BG2,
        fg=SUBTEXT,
        font=("Segoe UI", 9, "bold"),
        pady=7,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    drop_zone.pack(fill="x")
    animate_widget_color(drop_zone, "highlightbackground", ACCENT, 260, 12, BORDER)
    root.after(280, animate_widget_color, drop_zone, "highlightbackground", BORDER, 320, 12, ACCENT)

    topbar = tk.Frame(root, bg=CARD, padx=20, pady=13, highlightbackground=BORDER_SOFT, highlightthickness=1)
    topbar.pack(fill="x", padx=30, pady=(0, 14))

    scan_btn = tk.Button(
        topbar, text="⌁  SCAN SYSTEM", bg=GREEN, fg=TEXT,
        relief="flat", font=("Segoe UI", 9, "bold"), padx=15, pady=9,
        cursor="hand2", bd=0, command=start_auto_scan,
        activebackground=GREEN_HOVER, activeforeground=TEXT,
    )
    scan_btn.pack(side="left", padx=(0, 16))
    bind_animated_button(scan_btn, GREEN, GREEN_HOVER, TEXT, TEXT, sound=True)

    tk.Label(topbar, text="⌕  SEARCH", bg=CARD, fg=NEON, font=("Segoe UI", 9, "bold")).pack(side="left")

    search_var = tk.StringVar()
    search_var.trace_add("write", search_games)

    search_entry = tk.Entry(topbar, textvariable=search_var, bg=BG2, fg=TEXT, insertbackground=NEON, relief="flat", highlightbackground=BORDER, highlightthickness=1, font=("Segoe UI", 11))
    search_entry.pack(side="left", padx=12, ipady=8)
    search_entry.bind("<FocusIn>", lambda _event: animate_entry_focus(search_entry, True))
    search_entry.bind("<FocusOut>", lambda _event: animate_entry_focus(search_entry, False))

    tk.Label(topbar, text="NEW ENTRY", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(24, 6))

    name_entry = tk.Entry(topbar, bg=BG2, fg=TEXT, insertbackground=NEON, relief="flat", highlightbackground=BORDER, highlightthickness=1, font=("Segoe UI", 11))
    name_entry.pack(side="left", padx=5, ipady=8)
    name_entry.bind("<FocusIn>", lambda _event: animate_entry_focus(name_entry, True))
    name_entry.bind("<FocusOut>", lambda _event: animate_entry_focus(name_entry, False))

    add_btn = tk.Button(topbar, text="＋  ADD ENTRY", bg=ACCENT, fg=TEXT, relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=9, cursor="hand2", bd=0, command=add_item)
    add_btn.pack(side="left", padx=15)
    bind_animated_button(add_btn, ACCENT, ACCENT2, TEXT, TEXT)

    sort_btn = tk.Button(topbar, text="⇅  PINNED FIRST", bg=BG2, fg=SUBTEXT, relief="flat", font=("Segoe UI", 9, "bold"), padx=15, pady=9, cursor="hand2", bd=0, command=change_sort)
    sort_btn.pack(side="right", padx=10)
    bind_animated_button(sort_btn, BG2, CARD2, SUBTEXT, TEXT)
    startup_checkpoint(
        "COMMAND_CONTROLS",
        "READY",
        "system_scan=ready; search=ready; add_entry=ready; sort=ready; drop_zone=created",
    )

    container = tk.Frame(root, bg=BG)
    container.pack(fill="both", expand=True, padx=20, pady=(0, 5))

    canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
    canvas.pack(side="left", fill="both", expand=True)

    xvviix_style = ttk.Style(root)
    try:
        xvviix_style.theme_use("clam")
    except tk.TclError:
        pass
    xvviix_style.configure(
        "XVVIIX.Vertical.TScrollbar",
        troughcolor=BG, background=CARD2, bordercolor=BG,
        darkcolor=CARD2, lightcolor=CARD2, arrowcolor=SUBTEXT,
        relief="flat", width=10,
    )
    xvviix_style.map("XVVIIX.Vertical.TScrollbar", background=[("active", ACCENT)])
    scrollbar = ttk.Scrollbar(
        container, orient="vertical", command=canvas.yview,
        style="XVVIIX.Vertical.TScrollbar",
    )
    scrollbar.pack(side="right", fill="y", padx=(6, 0))

    canvas.configure(yscrollcommand=scrollbar.set)

    frame = tk.Frame(canvas, bg=BG)
    frame_id = canvas.create_window((0, 0), window=frame, anchor="nw")

    frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(frame_id, width=e.width))

    if DND_ACTIVE:
        try:
            for target in (root, drop_zone, canvas, frame):
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<DropEnter>>", handle_drop_enter)
                target.dnd_bind("<<DropLeave>>", handle_drop_leave)
                target.dnd_bind("<<Drop>>", handle_drop_event)
        except (tk.TclError, AttributeError) as exc:
            LOG.warning("Drag-and-drop registration failed: %s", exc)
            DND_ACTIVE = False
            reset_drop_status()

    startup_checkpoint(
        "CONTENT_CANVAS",
        "READY",
        "scroll_canvas=ready; responsive_frame=ready; themed_scrollbar=ready",
    )
    startup_checkpoint(
        "INPUT_INTEGRATIONS",
        "READY" if DND_ACTIVE else ("DEGRADED" if HAS_DND else "SKIPPED"),
        "drag_and_drop=registered" if DND_ACTIVE else ("drag_and_drop=registration_failed" if HAS_DND else "drag_and_drop=package_unavailable"),
    )

    root.bind("<Configure>", update_scale)
    root.bind_all(
        "<MouseWheel>",
        lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"),
    )

    stats_frame = tk.Frame(root, bg=CARD, pady=8, highlightbackground=BORDER_SOFT, highlightthickness=1)
    stats_frame.pack(side="bottom", fill="x")
    tk.Label(
        stats_frame, text="●  SYSTEM ONLINE", bg=CARD, fg=GREEN,
        font=("Segoe UI", 8, "bold"),
    ).pack(side="left", padx=24)
    stats_lbl = tk.Label(
        stats_frame, text="", bg=CARD, fg=NEON,
        font=("Segoe UI", 9, "bold"),
    )
    stats_lbl.pack(side="left", expand=True)
    tk.Label(
        stats_frame, text="F11  FULLSCREEN     ESC  EXIT", bg=CARD, fg=MUTED,
        font=("Consolas", 8, "bold"),
    ).pack(side="right", padx=24)
    startup_checkpoint(
        "STATUS_INTERFACE",
        "READY",
        "system state, aggregate statistics, fullscreen, and exit indicators created",
    )

    root.update_idletasks()
    enable_win11_round_corners(root)

    apply_topbar_scaling()
    refresh()
    startup_checkpoint(
        "INITIAL_RENDER",
        "READY",
        f"active_tab={active_tab}; visible_records={len(games)}; responsive_scale={current_scale:.2f}",
    )
    process_ui_queue()
    startup_checkpoint("UI_DISPATCH_QUEUE", "READY", "worker result queue scheduled on Tk thread")

    if hardware_monitor is not None:
        hardware_monitor.stop()
    hardware_monitor = None
    hardware_monitor_error = ""
    if HAS_HARDWARE_MONITOR:
        hardware_monitor_state = "standby"
        startup_checkpoint(
            "HARDWARE_MONITOR", "READY",
            "on-demand standby; zero telemetry overhead until Monitor or overlay opens",
        )
    else:
        hardware_monitor_state = "unavailable"
        startup_checkpoint(
            "HARDWARE_MONITOR", "SKIPPED", "integrated monitor backend or psutil unavailable"
        )

    if background_music.enabled:
        def start_audio_after_first_paint():
            threading.Thread(
                target=background_music.start,
                args=(True,),
                daemon=True,
                name="background-audio-loader",
            ).start()
        root.after(splash_duration + 120, start_audio_after_first_paint)
    else:
        startup_checkpoint("AUDIO_PLAYBACK", "SKIPPED", "disabled by launcher setting")

    if load_warnings:
        root.after(
            250,
            messagebox.showwarning,
            "Library recovery",
            "\n\n".join(load_warnings),
        )

    def close_launcher():
        startup_checkpoint("SHUTDOWN_REQUEST", "STARTED", "stopping monitors, audio, animations, and vault session")
        monitor_stop.set()
        if HAS_PSUTIL:
            account_tracked_processes(remove_all=True)
        if background_music is not None:
            background_music.stop()
        close_hardware_overlay()
        cancel_hardware_monitor_idle_stop()
        cancel_hardware_monitor_view_refresh()
        if hardware_monitor is not None:
            hardware_monitor.stop()
        cancel_all_animations()
        clear_vault_key()
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.protocol("WM_DELETE_WINDOW", close_launcher)

    if HAS_PSUTIL:
        monitor_stop.clear()
        monitor_thread = threading.Thread(
            target=monitor_running_apps, daemon=True, name="playtime-monitor",
        )
        monitor_thread.start()
        print("Monitor thread starting...", flush=True)
        monitor_status = "READY"
        monitor_detail = f"playtime-monitor alive={monitor_thread.is_alive()}"
    else:
        monitor_status = "SKIPPED"
        monitor_detail = "playtime monitor unavailable without psutil"
    startup_checkpoint(
        "ASYNC_SERVICES",
        monitor_status,
        f"{monitor_detail}; hardware_monitor={hardware_monitor_state}; audio={'scheduled' if background_music.enabled else 'disabled'}; recovery_notices={len(load_warnings)}",
    )

    print("BEFORE MAINLOOP", flush=True)
    startup_checkpoint(
        "MAIN_EVENT_LOOP",
        "READY",
        f"Tk callbacks armed; startup_checkpoints={len(startup_diagnostics) + 1}",
    )
    try:
        root.mainloop()
    except Exception as exc:
        startup_checkpoint("MAIN_EVENT_LOOP", "FAILED", f"fatal callback error: {exc}")
        LOG.exception("Fatal main-loop error")
        try:
            messagebox.showerror("XVVIIX Launcher", f"The launcher stopped unexpectedly:\n{exc}")
        except tk.TclError:
            pass
        close_hardware_overlay()
        cancel_hardware_monitor_idle_stop()
        cancel_hardware_monitor_view_refresh()
        if hardware_monitor is not None:
            hardware_monitor.stop()
        return 1
    close_hardware_overlay()
    cancel_hardware_monitor_idle_stop()
    cancel_hardware_monitor_view_refresh()
    if hardware_monitor is not None:
        hardware_monitor.stop()
    startup_checkpoint("MAIN_EVENT_LOOP", "STOPPED", "Tk event loop exited cleanly")
    return 0


def run():
    try:
        return main()
    except Exception as exc:
        startup_checkpoint("APPLICATION_STARTUP", "FAILED", f"unhandled exception: {exc}")
        LOG.exception("Launcher startup failed")
        try:
            if root is not None:
                root.destroy()
        except (AttributeError, tk.TclError):
            pass
        report_startup_error(f"The launcher could not start:\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
