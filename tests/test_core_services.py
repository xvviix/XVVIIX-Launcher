"""Focused tests for services now usable without the launcher UI."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from xvviix import models, utils
from xvviix.services import audio, hardware_monitor


class CoreServiceTests(unittest.TestCase):
    def test_percentage_bounds_and_cpu_normalization(self):
        monitor = hardware_monitor
        for value, expected in ((-1, 0.0), (40, 40.0), (300, 100.0), ("25", 25.0)):
            with self.subTest(value=value):
                self.assertEqual(monitor.monitor_clamp_percent(value), expected)
        for value in (None, "invalid", float("inf"), float("nan")):
            self.assertIsNone(monitor.monitor_clamp_percent(value))
        self.assertEqual(monitor.monitor_normalize_process_cpu(400, 8), 50.0)
        self.assertEqual(monitor.monitor_format_bytes(1024), "1.0 KB")

    def test_monitor_can_sample_and_stop_without_ui_or_external_probe(self):
        if hardware_monitor.psutil is None:
            self.skipTest("psutil unavailable")
        gpu = Mock()
        gpu.sample.return_value = {"available": False, "name": "test-only backend"}
        sampled = threading.Event()
        with (
            patch.object(hardware_monitor, "_MonitorGpuProbe", return_value=gpu),
            patch.object(
                hardware_monitor.socket,
                "create_connection",
                side_effect=AssertionError("No network probe expected"),
            ) as connection,
        ):
            service = hardware_monitor.HardwareMonitorService(interval=0.35)
            service._latency = Mock()
            service._latency.value.return_value = None
            original = service._sample

            def observed_sample():
                result = original()
                sampled.set()
                return result

            service._sample = observed_sample
            try:
                self.assertTrue(service.start())
                self.assertTrue(sampled.wait(8), "No successful telemetry sample")
            finally:
                service.stop()
            connection.assert_not_called()
        self.assertFalse(service.is_running())
        self.assertFalse(service._thread.is_alive())
        snapshot = service.snapshot()
        self.assertEqual(snapshot["status"], "online")
        self.assertLessEqual(
            len(snapshot["processes"]), hardware_monitor.MONITOR_PROCESS_SAFETY_LIMIT
        )
        for key in ("cpu", "memory", "storage"):
            self.assertGreaterEqual(snapshot[key]["percent"], 0)
            self.assertLessEqual(snapshot[key]["percent"], 100)
        snapshot["cpu"]["percent"] = -1
        self.assertGreaterEqual(service.snapshot()["cpu"]["percent"], 0)
        gpu.close.assert_called_once()

    def test_path_and_record_helpers_remain_platform_independent(self):
        self.assertEqual(
            utils.canonical_path(r'"C:\Games\Foo\..\GAME.EXE"'),
            utils.canonical_path("c:/games/game.exe"),
        )
        record = models.normalize_item(
            {"name": " Example ", "path": r"C:\Games\game.exe", "color": "#123456", "playtime": -1}
        )
        self.assertEqual(record["name"], "Example")
        self.assertEqual(record["playtime"], 0)
        self.assertEqual(record["color"], "#123456")
        with self.assertRaises(ValueError):
            models.normalize_item({"name": ""})

    def test_audio_failure_uses_callbacks_not_tk(self):
        with tempfile.TemporaryDirectory(prefix="xvviix-audio-test-") as directory:
            track = Path(directory) / "fake-track.ogg"
            track.write_bytes(b"synthetic file, not audio")
            notify, checkpoint = Mock(), Mock()
            with (
                patch.multiple(audio, pygame=None, HAS_PYGAME=True, _pygame_import_attempted=False),
                patch.object(audio, "load_optional_pygame", return_value=None),
            ):
                player = audio.BackgroundMusic(
                    str(track), on_unavailable=notify, checkpoint=checkpoint
                )
                self.assertFalse(player.start(startup_probe=True))
                self.assertFalse(player.available)
                player.stop()
            notify.assert_called_once_with()
            self.assertEqual(checkpoint.call_args.args[:2], ("AUDIO_PLAYBACK", "DEGRADED"))

    def test_audio_playback_and_cleanup_with_injected_backend(self):
        with tempfile.TemporaryDirectory(prefix="xvviix-audio-test-") as directory:
            track = Path(directory) / "fake-track.ogg"
            track.write_bytes(b"synthetic file, not audio")
            backend, checkpoint = Mock(), Mock()
            backend.mixer.get_init.return_value = True
            with (
                patch.multiple(
                    audio, pygame=backend, HAS_PYGAME=True, _pygame_import_attempted=True
                ),
                patch.object(audio, "load_optional_pygame", return_value=backend),
            ):
                player = audio.BackgroundMusic(
                    str(track), volume=0.4, title="fixture track", checkpoint=checkpoint
                )
                self.assertTrue(player.start(startup_probe=True))
                backend.mixer.music.load.assert_called_once_with(str(track))
                backend.mixer.music.set_volume.assert_called_once_with(0.4)
                backend.mixer.music.play.assert_called_once_with(loops=-1, fade_ms=700)
                self.assertEqual(checkpoint.call_args.args[:2], ("AUDIO_PLAYBACK", "READY"))
                player.stop()
                backend.mixer.music.unload.assert_called_once()
                backend.mixer.quit.assert_called_once()
                self.assertFalse(player._playing)
