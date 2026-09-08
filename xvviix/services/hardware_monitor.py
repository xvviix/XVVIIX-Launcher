"""On-demand telemetry backend. No Tk dependency and no independent UI/event loop."""

import copy
from collections import deque
import heapq
import logging
import os
import platform
import socket
import threading
import time
from typing import Any

try:
    import psutil
except (ImportError, OSError):
    psutil = None

LOG = logging.getLogger("xvviix_launcher")
pynvml = None
_monitor_pynvml_attempted = False

MONITOR_IS_WINDOWS = os.name == "nt"

TOP_PROCESS_LIMIT = 5
# Only the union of both top-five lists is retained in application snapshots.
MONITOR_PROCESS_SAFETY_LIMIT = TOP_PROCESS_LIMIT * 2


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
            if (
                self._pdh.PdhAddEnglishCounterW(
                    self._query, instance_path, 0, ctypes.byref(counter)
                )
                == 0
            ):
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
            result.update(
                {
                    "available": True,
                    "source": "NVML",
                    "name": str(raw_name),
                    "usage": monitor_clamp_percent(
                        utilization.gpu if utilization is not None else None
                    ),
                    "vram_used": used,
                    "vram_total": total,
                    "vram_percent": monitor_clamp_percent(
                        (used / total * 100.0) if used is not None and total else None
                    ),
                    "temperature": self._safe(
                        lambda: float(
                            pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                        )
                    ),
                    "clock_mhz": self._safe(
                        lambda: float(
                            pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                        )
                    ),
                    "power_w": self._safe(
                        lambda: float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
                    ),
                    "fan_percent": monitor_clamp_percent(
                        self._safe(lambda: pynvml.nvmlDeviceGetFanSpeed(handle))
                    ),
                }
            )
            return result
        usage = self._engine.sample()
        if usage is not None:
            result.update(
                {
                    "available": True,
                    "source": "Windows PDH",
                    "name": "Windows graphics adapter",
                    "usage": usage,
                }
            )
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
        self._process_sample_interval = 3.0
        self._process_sample_ready = False
        self._process_revision = 0
        self._top_processes = {"top_cpu": [], "top_memory": []}
        self._frequency = None
        self._last_frequency_sample = None
        self._disk_usage = None
        self._last_disk_usage_sample = None
        self._boot_time = self._safe_call(psutil.boot_time, time.time())
        self._system_process_count = 0
        self._cpu_history = deque(maxlen=40)
        self._overlay_latest = self._make_overlay_snapshot(self._latest)

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
            "cpu": {},
            "gpu": {},
            "memory": {},
            "storage": {},
            "network": {},
            "system": {},
            "processes": [],
            "user_process_total": 0,
            "top_cpu": [],
            "top_memory": [],
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
        # Published snapshots are never mutated. Copy outside the publication lock.
        with self._lock:
            snapshot = self._latest
        return copy.deepcopy(snapshot)

    def overlay_snapshot(self):
        """Small display-only snapshot: top-five summaries, with no executable paths."""
        with self._lock:
            snapshot = self._overlay_latest
        return copy.deepcopy(snapshot)

    @staticmethod
    def _top_process_summary(records):
        candidates = [
            record
            for record in records
            if record.get("pid", 0) > 0
            and str(record.get("name", "")).casefold()
            not in {"system idle process", "system idle", "idle"}
        ]

        def compact(record):
            return {
                "pid": max(0, int(record.get("pid") or 0)),
                "name": str(record.get("name") or "Unknown")[:80],
                "memory_bytes": max(0, int(record.get("memory_bytes") or 0)),
                "cpu_percent": monitor_clamp_percent(record.get("cpu_percent")) or 0.0,
                "memory_percent": monitor_clamp_percent(record.get("memory_percent")) or 0.0,
            }

        return {
            "top_cpu": [
                compact(record)
                for record in heapq.nlargest(
                    TOP_PROCESS_LIMIT,
                    candidates,
                    key=lambda item: (item.get("cpu_percent", 0), item.get("memory_bytes", 0)),
                )
            ],
            "top_memory": [
                compact(record)
                for record in heapq.nlargest(
                    TOP_PROCESS_LIMIT,
                    candidates,
                    key=lambda item: (item.get("memory_bytes", 0), item.get("cpu_percent", 0)),
                )
            ],
        }

    def _make_overlay_snapshot(self, snapshot):
        # All containers here are private to this published snapshot.
        return {
            "timestamp": snapshot.get("timestamp", 0.0),
            "status": snapshot.get("status", "starting"),
            "error": snapshot.get("error", ""),
            "cpu": {
                key: snapshot.get("cpu", {}).get(key)
                for key in ("percent", "current_mhz", "temperature")
            },
            "gpu": dict(snapshot.get("gpu", {})),
            "memory": dict(snapshot.get("memory", {})),
            "storage": dict(snapshot.get("storage", {})),
            "network": {
                key: snapshot.get("network", {}).get(key)
                for key in ("download_rate", "upload_rate", "latency_ms")
            },
            "system": {
                key: snapshot.get("system", {}).get(key)
                for key in ("uptime_seconds", "process_count")
            },
            "top_cpu": [dict(item) for item in self._top_processes["top_cpu"]],
            "top_memory": [dict(item) for item in self._top_processes["top_memory"]],
            "cpu_history": list(self._cpu_history),
        }

    def is_running(self):
        return bool(
            self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
        )

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
                    readings.append(
                        {
                            "group": str(group),
                            "sensor": entry.label or group,
                            "temperature": current,
                        }
                    )
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
                address.address
                for address in addresses.get(name, [])
                if address.family == socket.AF_INET and not address.address.startswith("127.")
            ]
            ipv4_count += len(ipv4)
            active.append(
                {
                    "name": name,
                    "speed_mbps": max(0, int(stat.speed or 0)),
                    "ipv4": ipv4[:4],
                }
            )
        return active[:32], ipv4_count

    def _sample_processes(self, memory_total):
        """Scan cheap counters periodically; retain only two fixed-size heaps.

        Discovering the top five still requires considering eligible processes.
        No executable path, creation time, thread count or process-state query is
        performed. Local reads avoid sharing mutable process.info dictionaries
        with the separate game-session watcher.
        """
        cpu_heap, memory_heap = [], []
        user_total = 0
        memory_total = max(1, int(memory_total or 1))
        wanted_user = self._username.casefold()
        for ordinal, process in enumerate(psutil.process_iter()):
            try:
                pid = int(process.pid)
                if pid <= 0:
                    continue
                with process.oneshot():
                    if wanted_user and str(process.username() or "").casefold() != wanted_user:
                        continue
                    user_total += 1
                    name = str(process.name() or "Unknown process")[:80]
                    if name.casefold() in {"system idle process", "system idle", "idle"}:
                        continue
                    cpu = (
                        monitor_normalize_process_cpu(
                            process.cpu_percent(interval=None), self._logical_cpus
                        )
                        or 0.0
                    )
                    memory_bytes = max(0, int(process.memory_info().rss))
                record = {
                    "pid": pid,
                    "name": name,
                    "cpu_percent": cpu,
                    "memory_bytes": memory_bytes,
                    "memory_percent": monitor_clamp_percent(memory_bytes / memory_total * 100.0)
                    or 0.0,
                }
                for heap, primary, secondary in (
                    (cpu_heap, cpu, memory_bytes),
                    (memory_heap, memory_bytes, cpu),
                ):
                    entry = (primary, secondary, -pid, ordinal, record)
                    if len(heap) < TOP_PROCESS_LIMIT:
                        heapq.heappush(heap, entry)
                    elif entry[:4] > heap[0][:4]:
                        heapq.heapreplace(heap, entry)
            except (psutil.Error, OSError, ValueError, TypeError, AttributeError):
                continue
        selected = {}
        for entry in sorted(cpu_heap, reverse=True) + sorted(memory_heap, reverse=True):
            record = entry[-1]
            selected[record["pid"]] = record
        # The third legacy internal tuple slot is no longer sampled or exposed.
        return list(selected.values()), user_total, 0

    def _sample(self):
        timestamp = time.time()
        now = time.monotonic()
        elapsed = max(0.001, now - self._previous_counter_time)
        per_cpu = [
            monitor_clamp_percent(value) or 0.0
            for value in psutil.cpu_percent(interval=None, percpu=True)
        ]
        overall = monitor_clamp_percent(sum(per_cpu) / len(per_cpu) if per_cpu else 0.0) or 0.0
        if self._last_frequency_sample is None or now - self._last_frequency_sample >= 2.0:
            self._frequency = self._safe_call(psutil.cpu_freq)
            self._last_frequency_sample = now
        frequency = self._frequency
        load_average = self._safe_call(os.getloadavg) if hasattr(os, "getloadavg") else None

        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        if self._last_disk_usage_sample is None or now - self._last_disk_usage_sample >= 5.0:
            self._disk_usage = psutil.disk_usage(self._root_mount())
            self._last_disk_usage_sample = now
        disk_usage = self._disk_usage
        disk_now = self._safe_call(psutil.disk_io_counters)
        network_now = self._safe_call(psutil.net_io_counters)

        def counter_rate(current, previous, field):
            if current is None or previous is None:
                return None
            return max(
                0.0, (float(getattr(current, field)) - float(getattr(previous, field))) / elapsed
            )

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
                "battery": None
                if battery is None
                else {
                    "percent": monitor_clamp_percent(battery.percent),
                    "plugged": bool(battery.power_plugged),
                    "seconds_left": int(battery.secsleft),
                },
                "temperatures": temperatures,
            }
            self._last_slow_sample = now

        if (
            now - self._last_process_sample >= self._process_sample_interval
            or not self._process_sample_ready
        ):
            self._process_cache = self._sample_processes(memory.total)
            self._last_process_sample = now
            self._process_sample_ready = True
            self._process_revision += 1
            self._top_processes = self._top_process_summary(self._process_cache[0])
            self._system_process_count = len(psutil.pids())
        processes, user_process_total, _unused_threads = self._process_cache
        boot_time = self._boot_time
        gpu = self._gpu.sample()
        cpu_temperature = None
        temperatures = self._slow_cache.get("temperatures", [])
        cpu_sensor_markers = ("cpu", "coretemp", "k10temp", "zen", "tctl", "package", "acpi")
        cpu_temperatures = [
            entry["temperature"]
            for entry in temperatures
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
                "process_count": self._system_process_count,
                "user_process_count": user_process_total,
                "battery": self._slow_cache.get("battery"),
                "temperatures": self._slow_cache.get("temperatures", []),
            },
            "processes": processes,
            "top_cpu": [dict(row) for row in self._top_processes["top_cpu"]],
            "top_memory": [dict(row) for row in self._top_processes["top_memory"]],
            "process_revision": self._process_revision,
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
                snapshot.update(
                    {"timestamp": time.time(), "status": "degraded", "error": str(exc)[:512]}
                )
            if snapshot.get("status") == "online":
                self._cpu_history.append(
                    (snapshot["timestamp"], snapshot["cpu"].get("percent", 0.0))
                )
            overlay = self._make_overlay_snapshot(snapshot)
            with self._lock:
                self._latest = snapshot
                self._overlay_latest = overlay
            delay = max(0.0, self.interval - (time.monotonic() - started))
            self._stop.wait(delay)
