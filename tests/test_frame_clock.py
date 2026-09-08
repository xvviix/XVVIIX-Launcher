"""Deterministic frame pacing and scoped Windows timer tests; no Tk required."""

import os
import unittest
from unittest.mock import Mock

from xvviix.ui.frame_clock import FrameClock, WindowsTimerResolution, smooth_towards


class FakeScheduler:
    def __init__(self):
        self.now = 0.0
        self.jobs = {}
        self.serial = 0

    def after(self, delay, callback):
        self.serial += 1
        self.jobs[self.serial] = (delay, callback)
        return self.serial

    def cancel(self, job):
        self.jobs.pop(job, None)

    def tick(self):
        job = next(iter(self.jobs))
        delay, callback = self.jobs.pop(job)
        self.now += delay / 1000
        callback()


class FrameClockTests(unittest.TestCase):
    def make_clock(self, draw=None):
        self.scheduler = FakeScheduler()
        self.timer = Mock(active=False)
        return FrameClock(
            self.scheduler.after,
            self.scheduler.cancel,
            draw or Mock(),
            clock=lambda: self.scheduler.now,
            timer=self.timer,
            fps=60,
        )

    def test_sixty_hz_uses_deadlines_and_keeps_only_one_pending_job(self):
        clock = self.make_clock()
        clock.start()
        clock.start()
        for _ in range(120):
            self.assertEqual(len(self.scheduler.jobs), 1)
            self.scheduler.tick()
        self.assertAlmostEqual(clock.statistics()["callback_fps"], 60, delta=0.2)
        self.timer.acquire.assert_called_once()
        clock.stop()
        self.assertEqual(self.scheduler.jobs, {})

    def test_slow_frames_skip_deadlines_instead_of_queuing_catch_up_frames(self):
        def slow_draw(_now, _delta):
            self.scheduler.now += 0.040

        clock = self.make_clock(slow_draw)
        clock.start()
        for _ in range(5):
            self.scheduler.tick()
            self.assertEqual(len(self.scheduler.jobs), 1)
            self.assertGreater(next(iter(self.scheduler.jobs.values()))[0], 0)
        self.assertGreater(clock.missed_deadlines, 0)
        clock.stop()

    def test_stop_inside_draw_does_not_reschedule(self):
        clock = self.make_clock()
        clock.draw = lambda _now, _delta: clock.stop()
        clock.start()
        self.scheduler.tick()
        self.assertFalse(clock.running)
        self.assertFalse(self.scheduler.jobs)

    def test_draw_error_releases_timer_and_stops(self):
        clock = self.make_clock(Mock(side_effect=ValueError("synthetic drawing failure")))
        clock.start()
        with self.assertRaises(ValueError):
            self.scheduler.tick()
        self.assertFalse(clock.running)
        self.assertIsNone(clock.job)
        self.timer.release.assert_called_once()

    def test_resume_does_not_count_hidden_time_as_bad_frame_rate(self):
        clock = self.make_clock()
        clock.start()
        for _ in range(10):
            self.scheduler.tick()
        clock.stop()
        self.scheduler.now += 60
        clock.start()
        for _ in range(10):
            self.scheduler.tick()
        self.assertAlmostEqual(clock.statistics()["callback_fps"], 60, delta=0.3)
        clock.stop()

    def test_smoothing_is_frame_rate_independent_and_has_no_overshoot(self):
        values = []
        for fps in (30, 60, 120):
            current = 0.0
            for _ in range(fps // 5):
                current = smooth_towards(current, 80.0, 1 / fps)
                self.assertGreaterEqual(current, 0)
                self.assertLessEqual(current, 80)
            values.append(current)
        self.assertAlmostEqual(values[0], values[1], places=8)
        self.assertAlmostEqual(values[1], values[2], places=8)
        self.assertEqual(smooth_towards(80, None, 0.1), 0)


class TimerResolutionTests(unittest.TestCase):
    def test_windows_timer_request_is_balanced_and_idempotent(self):
        backend = Mock()
        backend.timeBeginPeriod.return_value = 0
        timer = WindowsTimerResolution(is_windows=True, backend=backend)
        self.assertTrue(timer.acquire())
        self.assertTrue(timer.acquire())
        timer.release()
        timer.release()
        backend.timeBeginPeriod.assert_called_once_with(1)
        backend.timeEndPeriod.assert_called_once_with(1)
        self.assertFalse(timer.active)

    def test_failed_timer_request_has_no_unmatched_release(self):
        backend = Mock()
        backend.timeBeginPeriod.return_value = 1
        timer = WindowsTimerResolution(is_windows=True, backend=backend)
        self.assertFalse(timer.acquire())
        timer.release()
        backend.timeEndPeriod.assert_not_called()

    def test_non_windows_never_calls_multimedia_timer(self):
        backend = Mock()
        timer = WindowsTimerResolution(is_windows=False, backend=backend)
        self.assertFalse(timer.acquire())
        timer.release()
        backend.timeBeginPeriod.assert_not_called()
        backend.timeEndPeriod.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Requires the real Windows multimedia timer API")
class NativeTimerTests(unittest.TestCase):
    def test_native_timer_request_and_release(self):
        timer = WindowsTimerResolution()
        try:
            self.assertTrue(timer.acquire())
        finally:
            timer.release()
        self.assertFalse(timer.active)
