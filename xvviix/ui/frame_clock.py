"""Deadline-paced UI frames, independent of telemetry sampling."""

from collections import deque
import ctypes
import math
import os
import time


class WindowsTimerResolution:
    """Request fine timers only while a visible animation is actually running."""

    def __init__(self, *, is_windows=None, backend=None):
        self.is_windows = os.name == "nt" if is_windows is None else bool(is_windows)
        self.backend = backend
        self.active = False

    def acquire(self):
        if self.active or not self.is_windows:
            return self.active
        try:
            if self.backend is None:
                self.backend = ctypes.WinDLL("winmm", use_last_error=True)
                for name in ("timeBeginPeriod", "timeEndPeriod"):
                    function = getattr(self.backend, name)
                    function.argtypes = [ctypes.c_uint]
                    function.restype = ctypes.c_uint
            self.active = self.backend.timeBeginPeriod(1) == 0
        except (AttributeError, OSError):
            self.active = False
        return self.active

    def release(self):
        if not self.active:
            return
        self.active = False
        try:
            self.backend.timeEndPeriod(1)
        except (AttributeError, OSError):
            pass


class FrameClock:
    """One scheduled frame at a time; skip missed deadlines instead of building a backlog."""

    def __init__(self, schedule, cancel, draw, *, fps=60, clock=time.perf_counter, timer=None):
        self.schedule = schedule
        self.cancel = cancel
        self.draw = draw
        self.clock = clock
        self.fps = max(1, min(120, int(fps)))
        self.interval = 1.0 / self.fps
        self.timer = timer if timer is not None else WindowsTimerResolution()
        self.running = False
        self.job = None
        self._last = 0.0
        self._deadline = 0.0
        self.frame_times = deque(maxlen=600)
        self.draw_times_ms = deque(maxlen=600)
        self.frame_count = 0
        self.missed_deadlines = 0

    def start(self):
        if self.running:
            return
        self.running = True
        self.frame_times.clear()
        self.draw_times_ms.clear()
        self._last = self.clock()
        self._deadline = self._last + self.interval
        self.timer.acquire()
        try:
            self.job = self.schedule(max(1, round(self.interval * 1000)), self._tick)
        except Exception:
            self.running = False
            self.timer.release()
            raise

    def stop(self):
        self.running = False
        if self.job is not None:
            try:
                self.cancel(self.job)
            except Exception:
                pass
            self.job = None
        self.timer.release()

    def _tick(self):
        self.job = None
        if not self.running:
            return
        now = self.clock()
        delta = max(0.0, min(0.1, now - self._last))
        self._last = now
        try:
            self.draw(now, delta)
        except Exception:
            self.stop()
            raise
        finished = self.clock()
        self.frame_count += 1
        self.frame_times.append(now)
        self.draw_times_ms.append(max(0.0, (finished - now) * 1000))
        if not self.running:
            return
        self._deadline += self.interval
        if self._deadline <= finished:
            skipped = int((finished - self._deadline) / self.interval) + 1
            self.missed_deadlines += skipped
            self._deadline += skipped * self.interval
        delay = max(1, int(round((self._deadline - finished) * 1000)))
        try:
            self.job = self.schedule(delay, self._tick)
        except Exception:
            self.stop()
            raise

    def statistics(self):
        times = list(self.frame_times)
        draws = sorted(self.draw_times_ms)
        intervals = sorted((b - a) * 1000 for a, b in zip(times, times[1:]))
        elapsed = times[-1] - times[0] if len(times) > 1 else 0.0
        return {
            "target_fps": self.fps,
            "callback_fps": (len(times) - 1) / elapsed if elapsed > 0 else 0.0,
            "frames": self.frame_count,
            "draw_ms_p95": draws[min(len(draws) - 1, int(len(draws) * 0.95))] if draws else 0.0,
            "frame_interval_ms_p95": intervals[min(len(intervals) - 1, int(len(intervals) * 0.95))]
            if intervals
            else 0.0,
            "missed_deadlines": self.missed_deadlines,
            "fine_timer_active": self.timer.active,
        }


def smooth_towards(current, target, delta, speed=15.0):
    """Frame-rate-independent damping, with neither overshoot nor per-frame allocations."""
    if target is None:
        return 0.0
    value = current + (target - current) * (1.0 - math.exp(-speed * max(0.0, delta)))
    return target if abs(value - target) < 0.02 else value
