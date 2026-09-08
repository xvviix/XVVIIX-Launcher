"""Real-Tk tests for the compact overlay; no live sensor collection or networking."""

import time
import tkinter as tk
import unittest
from unittest.mock import Mock

from xvviix.ui.lifecycle import cancel_pending_callbacks
from xvviix.ui.monitor_overlay import GameMonitorOverlay, percent_text, rate_text


def sample_snapshot(cpu=35, gpu=18):
    now = time.time()
    gb = 1024**3
    return {
        "timestamp": now,
        "status": "online",
        "cpu": {"percent": cpu, "current_mhz": 2500, "temperature": None},
        "gpu": {
            "usage": gpu,
            "vram_used": 2 * gb,
            "vram_total": 8 * gb,
            "temperature": 40,
            "clock_mhz": 450,
            "power_w": 27,
            "fan_percent": 0,
        },
        "memory": {
            "percent": 58,
            "used": 9.1 * gb,
            "total": 15.8 * gb,
            "swap_percent": 4,
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
        "system": {"uptime_seconds": 11340, "process_count": 249},
        "top_cpu": [
            {"name": "game.exe", "cpu_percent": 8},
            {"name": "browser.exe", "cpu_percent": 3},
        ],
        "top_memory": [
            {"name": "game.exe", "memory_percent": 4},
            {"name": "browser.exe", "memory_percent": 3},
        ],
        "cpu_history": [(now - 31 + i, float(i % 20 + 5)) for i in range(32)],
    }


class MonitorOverlayUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda kind, value, trace: self.errors.append(value)
        self.view = None
        self.provider = Mock(side_effect=sample_snapshot)

    def tearDown(self):
        if self.view is not None:
            self.view.destroy()
        cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def open(self):
        self.view = GameMonitorOverlay(self.root, self.provider, scale=1)
        self.pump(70)
        return self.view

    def pump(self, milliseconds):
        self.root.after(milliseconds, self.root.quit)
        self.root.mainloop()

    def test_reference_sections_fit_and_canvas_items_are_reused(self):
        view = self.open()
        self.assertEqual((view.window.winfo_width(), view.window.winfo_height()), (282, 566))
        for name in (
            "cpu_label",
            "gpu_label",
            "ram_label",
            "swap_label",
            "disk_label",
            "net_label",
        ):
            self.assertTrue(view.canvas.itemcget(name, "text"))
        count = len(view.canvas.find_all())
        for _ in range(8):
            view.poll_now()
        self.pump(80)
        self.assertEqual(len(view.canvas.find_all()), count)
        for name in ("top_cpu_name_0", "top_memory_name_0", "gpu_detail", "cpu_value", "ram_value"):
            box = view.canvas.bbox(name)
            self.assertGreaterEqual(box[0], 5)
            self.assertLessEqual(box[2], 277)
            self.assertLessEqual(box[3], 561)

    def test_scaled_overlay_keeps_detail_text_inside_the_window(self):
        self.view = GameMonitorOverlay(self.root, self.provider, scale=1.5)
        self.pump(80)
        self.assertEqual(
            (self.view.window.winfo_width(), self.view.window.winfo_height()), (423, 849)
        )
        for name in ("gpu_detail", "top_cpu_name_0", "top_memory_name_0", "ram_value"):
            box = self.view.canvas.bbox(name)
            self.assertGreaterEqual(box[0], 6)
            self.assertLessEqual(box[2], 420)
            self.assertLessEqual(box[3], 845)

    def test_opacity_is_lowered_and_can_be_adjusted_without_changing_canvas_items(self):
        view = self.open()
        self.assertAlmostEqual(view.opacity, 0.80)
        count = len(view.canvas.find_all())
        notify = Mock()
        view.on_opacity = notify
        view.set_opacity(0.65)
        self.assertAlmostEqual(view.opacity, 0.65)
        self.assertAlmostEqual(float(view.window.attributes("-alpha")), 0.65, places=2)
        notify.assert_called_once_with(0.65)
        view.set_opacity(0)
        self.assertEqual(view.opacity, 0.55)
        view.set_opacity(float("nan"))
        self.assertEqual(view.opacity, 0.80)
        self.assertEqual(len(view.canvas.find_all()), count)

    def test_only_five_processes_per_section_are_displayed_in_overlay(self):
        snapshot = sample_snapshot()
        snapshot["top_cpu"] = [
            {"name": f"cpu-{index}.exe", "cpu_percent": index + 1} for index in range(12)
        ]
        snapshot["top_memory"] = [
            {"name": f"ram-{index}.exe", "memory_percent": index + 1} for index in range(12)
        ]
        self.provider.side_effect = lambda: snapshot
        view = self.open()
        for prefix in ("top_cpu", "top_memory"):
            for index in range(5):
                self.assertTrue(view.canvas.itemcget(f"{prefix}_name_{index}", "text"))
                self.assertLessEqual(view.canvas.bbox(f"{prefix}_value_{index}")[3], 561)
            self.assertFalse(view.canvas.find_withtag(f"{prefix}_name_5"))

    def test_animation_frames_do_not_read_sensors(self):
        view = self.open()
        self.pump(600)
        self.assertGreater(view.frame_clock.frame_count, 5)
        self.assertLessEqual(self.provider.call_count, 3)
        self.assertGreater(view.frame_clock.frame_count, self.provider.call_count * 3)

    def test_bars_interpolate_rather_than_jump_to_new_readings(self):
        self.provider.side_effect = lambda: sample_snapshot(cpu=0)
        view = self.open()
        self.provider.side_effect = lambda: sample_snapshot(cpu=80)
        view.poll_now()
        self.assertEqual(view._current["cpu"], 0)
        self.pump(80)
        self.assertGreater(view._current["cpu"], 0)
        self.assertLess(view._current["cpu"], 80)
        self.pump(550)
        self.assertAlmostEqual(view._current["cpu"], 80, delta=0.2)

    def test_hidden_overlay_stops_polling_and_rendering_until_mapped_again(self):
        view = self.open()
        view.window.withdraw()
        self.pump(50)
        self.assertFalse(view.visible)
        self.assertFalse(view.frame_clock.running)
        frames, polls = view.frame_clock.frame_count, self.provider.call_count
        self.pump(550)
        self.assertEqual(view.frame_clock.frame_count, frames)
        self.assertEqual(self.provider.call_count, polls)
        view.window.deiconify()
        self.pump(100)
        self.assertTrue(view.visible)
        self.assertTrue(view.frame_clock.running)
        self.assertGreater(self.provider.call_count, polls)

    def test_compact_mode_keeps_text_but_pauses_animation(self):
        view = self.open()
        count = len(view.canvas.find_all())
        view.toggle_compact()
        self.pump(50)
        self.assertEqual(view.window.winfo_height(), 110)
        self.assertFalse(view.frame_clock.running)
        frames = view.frame_clock.frame_count
        self.pump(150)
        self.assertEqual(view.frame_clock.frame_count, frames)
        self.assertEqual(view.canvas.itemcget("compact_summary", "state"), "normal")
        self.assertEqual(view.canvas.itemcget("compact_network", "state"), "normal")
        self.assertIn("KB/s", view.canvas.itemcget("compact_network", "text"))
        view.toggle_compact()
        self.pump(100)
        self.assertEqual(view.window.winfo_height(), 566)
        self.assertTrue(view.frame_clock.running)
        self.assertEqual(len(view.canvas.find_all()), count)

    def test_missing_or_stale_telemetry_is_not_faked(self):
        self.provider.side_effect = lambda: None
        view = self.open()
        self.assertFalse(view.frame_clock.running)
        self.assertEqual(view.canvas.itemcget("gpu_value", "text"), "--")
        stale = sample_snapshot()
        stale["timestamp"] = time.time() - 60
        self.provider.side_effect = lambda: stale
        view.poll_now()
        self.assertFalse(view.frame_clock.running)
        self.assertIn("UNAVAILABLE", view.canvas.itemcget("status", "text"))

    def test_close_cancels_both_schedules_and_releases_frame_timer(self):
        view = self.open()
        view.destroy()
        frames, polls = view.frame_clock.frame_count, self.provider.call_count
        self.pump(550)
        self.assertTrue(view.closed)
        self.assertIsNone(view.frame_clock.job)
        self.assertIsNone(view._poll_job)
        self.assertFalse(view.frame_clock.timer.active)
        self.assertEqual(view.frame_clock.frame_count, frames)
        self.assertEqual(self.provider.call_count, polls)

    def test_percentages_are_bounded_and_unsupported_values_stay_unknown(self):
        self.assertEqual(percent_text(1090), "100%")
        self.assertEqual(percent_text(None), "--")
        self.assertEqual(percent_text(float("nan")), "--")
        self.assertEqual(rate_text(None), "--")
