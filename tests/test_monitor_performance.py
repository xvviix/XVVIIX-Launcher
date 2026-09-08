"""Telemetry work stays bounded and independent of animation frame rate."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xvviix.services import hardware_monitor as monitor


@unittest.skipIf(monitor.psutil is None, "psutil unavailable")
class MonitorPerformanceTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor, "_MonitorGpuProbe", return_value=Mock()):
            self.service = monitor.HardwareMonitorService(interval=1)
        self.service._gpu.sample.return_value = {"available": False, "usage": None}
        self.addCleanup(self.service.stop)

    def test_overlay_snapshot_is_bounded_and_omits_process_paths_and_usernames(self):
        records = [
            {
                "pid": index + 1,
                "name": f"fixture-{index}.exe",
                "cpu_percent": index % 100,
                "memory_percent": index % 30,
                "memory_bytes": index * 1024,
                "executable": "private synthetic path",
                "username": "test-only",
            }
            for index in range(512)
        ]
        self.service._top_processes = self.service._top_process_summary(records)
        snapshot = {
            **self.service._latest,
            "timestamp": 1,
            "cpu": {"percent": 20, "per_cpu": [20] * 128},
            "processes": records,
        }
        self.service._overlay_latest = self.service._make_overlay_snapshot(snapshot)
        light = self.service.overlay_snapshot()
        self.assertNotIn("processes", light)
        self.assertNotIn("static", light)
        self.assertNotIn("per_cpu", light["cpu"])
        for key in ("top_cpu", "top_memory"):
            self.assertEqual(len(light[key]), 5)
            for record in light[key]:
                self.assertEqual(
                    set(record), {"pid", "name", "memory_bytes", "cpu_percent", "memory_percent"}
                )
        light["cpu"]["percent"] = -99
        self.assertEqual(self.service.overlay_snapshot()["cpu"]["percent"], 20)

    def test_idle_process_is_not_reported_as_a_top_cpu_consumer(self):
        result = self.service._top_process_summary(
            [
                {"pid": 0, "name": "System Idle Process", "cpu_percent": 1090, "memory_bytes": 0},
                {
                    "pid": 1,
                    "name": "game.exe",
                    "cpu_percent": 25,
                    "memory_percent": 5,
                    "memory_bytes": 100,
                },
            ]
        )
        self.assertEqual([row["name"] for row in result["top_cpu"]], ["game.exe"])
        self.assertEqual(result["top_cpu"][0]["cpu_percent"], 25)

    def test_full_snapshot_copy_does_not_hold_publication_lock(self):
        real_copy = monitor.copy.deepcopy

        def check_lock(value):
            acquired = self.service._lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                self.service._lock.release()
            return real_copy(value)

        with patch.object(monitor.copy, "deepcopy", side_effect=check_lock):
            self.service.snapshot()
            self.service.overlay_snapshot()

    def test_frequency_disk_and_even_empty_process_results_have_independent_cadences(self):
        frequency = SimpleNamespace(current=2500, max=4000)
        disk = SimpleNamespace(percent=50, used=500, free=500, total=1000)
        clock = [100.0]
        with (
            patch.object(monitor.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(monitor.psutil, "cpu_freq", return_value=frequency) as freq,
            patch.object(monitor.psutil, "disk_usage", return_value=disk) as usage,
            patch.object(self.service, "_sample_processes", return_value=([], 0, 0)) as processes,
            patch.object(self.service, "_sample_interfaces", return_value=([], 0)),
            patch.object(self.service, "_sample_temperatures", return_value=[]),
        ):
            for now in (100, 100.5, 101, 101.5):
                clock[0] = now
                self.service._sample()
            self.assertEqual(freq.call_count, 1)
            self.assertEqual(usage.call_count, 1)
            self.assertEqual(processes.call_count, 1)
            clock[0] = 102.1
            self.service._sample()
            self.assertEqual(freq.call_count, 2)
            self.assertEqual(processes.call_count, 1)
            self.assertEqual(usage.call_count, 1)
            clock[0] = 103.1
            self.service._sample()
            self.assertEqual(processes.call_count, 2)
            clock[0] = 105.1
            self.service._sample()
            self.assertEqual(usage.call_count, 2)
