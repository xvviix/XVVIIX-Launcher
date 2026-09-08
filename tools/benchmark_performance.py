"""Reproducible display/snapshot microbenchmarks using explicitly synthetic data.

Run from the project root with a desktop display, or through xvfb-run on Linux.
This is not a benchmark of game FPS, vsync presentation, GPU drivers or disk speed.
"""

import argparse
from collections import deque
import copy
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from xvviix.services import hardware_monitor
from xvviix.ui.lifecycle import cancel_pending_callbacks
from xvviix.ui.monitor_overlay import GameMonitorOverlay
import tkinter as tk


class DemoTelemetry:
    """1 Hz synthetic source; UI polling and 60 Hz drawing must not increase this rate."""

    def __init__(self):
        self.start = time.monotonic()
        self.last = -1.0
        self.cached = None
        self.samples = 0
        now = time.time()
        self.history = deque(
            ((now - 32 + i, 22 + 18 * math.sin(i * 0.25)) for i in range(32)), maxlen=40
        )

    def __call__(self):
        elapsed = time.monotonic() - self.start
        if self.cached is not None and elapsed - self.last < 1.0:
            return copy.deepcopy(self.cached)
        self.last = elapsed
        self.samples += 1
        gb = 1024**3
        timestamp = time.time()
        cpu = 23 + 18 * math.sin(elapsed * 0.8)
        gpu = 37 + 27 * math.sin(elapsed * 0.6 + 0.3)
        memory_pct = 57 + 3 * math.sin(elapsed * 0.17)
        self.history.append((timestamp, cpu))
        self.cached = {
            "demo": True,
            "timestamp": timestamp,
            "status": "online",
            "cpu": {"percent": cpu, "current_mhz": 2500, "temperature": None},
            "gpu": {
                "usage": gpu,
                "vram_used": 2 * gb,
                "vram_total": 8 * gb,
                "temperature": 41,
                "clock_mhz": 450,
                "power_w": 27,
                "fan_percent": 0,
            },
            "memory": {
                "percent": memory_pct,
                "used": memory_pct / 100 * 15.8 * gb,
                "total": 15.8 * gb,
                "swap_percent": 0.5 / 13.5 * 100,
                "swap_used": 0.5 * gb,
                "swap_total": 13.5 * gb,
            },
            "storage": {
                "percent": 88,
                "used": 418 * gb,
                "total": 476 * gb,
                "read_rate": 68 * 1024,
                "write_rate": 521 * 1024,
            },
            "network": {"download_rate": 5 * 1024, "upload_rate": 5 * 1024, "latency_ms": 106},
            "system": {"uptime_seconds": 11340 + int(elapsed), "process_count": 249},
            "top_cpu": [
                {"name": "game.exe", "cpu_percent": 8.2},
                {"name": "browser.exe", "cpu_percent": 3.1},
                {"name": "launcher.exe", "cpu_percent": 0.8},
                {"name": "example.exe", "cpu_percent": 0.5},
                {"name": "demo.exe", "cpu_percent": 0.2},
            ],
            "top_memory": [
                {"name": "game.exe", "memory_percent": 4},
                {"name": "browser.exe", "memory_percent": 3.2},
                {"name": "example.exe", "memory_percent": 2.1},
                {"name": "demo.exe", "memory_percent": 1.0},
                {"name": "launcher.exe", "memory_percent": 0.5},
            ],
            "cpu_history": list(self.history),
        }
        return copy.deepcopy(self.cached)


def snapshot_benchmark(iterations=300):
    # Constructor is used normally, but GPU probing is stubbed and no service worker is started.
    with patch.object(hardware_monitor, "_MonitorGpuProbe", return_value=Mock()):
        service = hardware_monitor.HardwareMonitorService(interval=1.0)
    try:
        fixture = DemoTelemetry()()
        records = [
            {
                "pid": index + 1,
                "name": f"Synthetic-{index}.exe",
                "cpu_percent": float(index % 25),
                "memory_percent": float(index % 7),
                "memory_bytes": index * 1024,
                "threads": 8,
                "status": "running",
                "username": "test-only",
                "executable": r"C:\TestFixtures\program.exe",
                "create_time": 1.0,
            }
            for index in range(512)
        ]
        service._top_processes = service._top_process_summary(records)
        selected = {row["pid"]: row for group in service._top_processes.values() for row in group}
        service._cpu_history.extend(fixture["cpu_history"])
        full = {**service._latest, **fixture, "processes": list(selected.values())}
        for field in ("cpu_history", "top_cpu", "top_memory", "demo"):
            full.pop(field, None)
        full["cpu"]["per_cpu"] = [25.0] * 128
        with service._lock:
            service._latest = full
            service._overlay_latest = service._make_overlay_snapshot(full)
        result = {}
        for name, callback in (("full", service.snapshot), ("overlay", service.overlay_snapshot)):
            durations = []
            for _ in range(iterations):
                start = time.perf_counter()
                callback()
                durations.append((time.perf_counter() - start) * 1000)
            result[name + "_ms_median"] = statistics.median(durations)
            result[name + "_ms_p95"] = sorted(durations)[int(len(durations) * 0.95)]
        result["input_candidates"] = 512
        result["process_rows"] = len(selected)
        result["top_rows_per_category"] = 5
        result["iterations"] = iterations
        result["median_copy_speed_ratio"] = result["full_ms_median"] / max(
            0.000001, result["overlay_ms_median"]
        )
        return result
    finally:
        service.stop()


def overlay_benchmark(seconds, fps, scale, screenshot=None):
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
    provider = DemoTelemetry()
    overlay = GameMonitorOverlay(root, provider, fps=fps, scale=scale, title="GAME MONITOR")
    results = {}
    started = {}

    def begin():
        overlay.frame_clock.frame_times.clear()
        overlay.frame_clock.draw_times_ms.clear()
        overlay.frame_clock.frame_count = 0
        overlay.frame_clock.missed_deadlines = 0
        overlay.poll_count = 0
        started.update(wall=time.perf_counter(), cpu=time.process_time(), samples=provider.samples)
        root.after(round(seconds * 1000), finish)

    def finish():
        elapsed = time.perf_counter() - started["wall"]
        cpu_time = time.process_time() - started["cpu"]
        results.update(overlay.statistics())
        results.update(
            elapsed_seconds=elapsed,
            python_cpu_percent_of_one_core=100 * cpu_time / elapsed,
            synthetic_sensor_samples=provider.samples - started["samples"],
            window_width=overlay.window.winfo_width(),
            window_height=overlay.window.winfo_height(),
            data_source="synthetic demo, not the user's machine",
        )
        if screenshot:
            from PIL import ImageGrab

            output = Path(screenshot)
            output.parent.mkdir(parents=True, exist_ok=True)
            x, y = overlay.window.winfo_rootx(), overlay.window.winfo_rooty()
            ImageGrab.grab(
                bbox=(x, y, x + overlay.window.winfo_width(), y + overlay.window.winfo_height())
            ).save(output)
        overlay.destroy()
        cancel_pending_callbacks(root)
        root.destroy()

    root.after(
        1000, begin
    )  # Exclude font/window creation and the initial animation from the interval.
    root.mainloop()
    if errors:
        raise RuntimeError("Benchmark UI errors: " + "; ".join(errors))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 120:
        parser.error("--seconds must be between 2 and 120")
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system(),
            "tk": tk.TkVersion,
        },
        "overlay": overlay_benchmark(args.seconds, args.fps, args.scale, args.screenshot),
        "snapshot_copy": snapshot_benchmark(),
        "notes": [
            "Callback frequency is not a vsync/presented-frame measurement and is not game FPS.",
            "Process CPU excludes other processes, including the Linux display server.",
            "Synthetic inputs keep the workload repeatable; native drivers and game load can change results.",
        ],
    }
    text = json.dumps(result, indent=2)
    print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
