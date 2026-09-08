"""Top-five retention, overlay preferences and removal of automatic crash reports."""

from contextlib import nullcontext
from pathlib import Path
import json
import unittest
from unittest.mock import Mock, patch

from xvviix.services import hardware_monitor as monitor
from tests.support import IsolatedLauncherTest, PROJECT_ROOT


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid

    def oneshot(self):
        return nullcontext()

    def username(self):
        return "test-user"

    def name(self):
        return f"fixture-{self.pid}.exe"

    def cpu_percent(self, interval=None):
        return self.pid / 10

    def memory_info(self):
        return type("Memory", (), {"rss": (1001 - self.pid) * 1024})()

    @property
    def info(self):
        raise AssertionError("Do not use shared process.info dictionaries")

    def exe(self):
        raise AssertionError("Top-five monitoring must not query executable paths")

    def create_time(self):
        raise AssertionError("Top-five monitoring must not query process creation time")

    def num_threads(self):
        raise AssertionError("Top-five monitoring must not count every process's threads")

    def status(self):
        raise AssertionError("Top-five monitoring must not query process state")


@unittest.skipIf(monitor.psutil is None, "psutil unavailable")
class TopFiveSamplingTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor, "_MonitorGpuProbe", return_value=Mock()):
            self.service = monitor.HardwareMonitorService(interval=1)
        self.service._username = "test-user"
        self.service._logical_cpus = 4
        self.addCleanup(self.service.stop)

    def test_top_five_can_come_from_after_the_old_512_record_cutoff(self):
        with patch.object(
            monitor.psutil,
            "process_iter",
            return_value=iter(FakeProcess(pid) for pid in range(1, 1001)),
        ) as iterator:
            records, eligible, _unused = self.service._sample_processes(16 * 1024**3)
        iterator.assert_called_once_with()
        self.assertEqual(eligible, 1000)
        self.assertLessEqual(len(records), 10)
        summary = self.service._top_process_summary(records)
        self.assertEqual([row["pid"] for row in summary["top_cpu"]], [1000, 999, 998, 997, 996])
        self.assertEqual([row["pid"] for row in summary["top_memory"]], [1, 2, 3, 4, 5])
        for row in records:
            self.assertEqual(
                set(row), {"pid", "name", "cpu_percent", "memory_bytes", "memory_percent"}
            )

    def test_current_user_filter_is_preserved_without_retaining_usernames(self):
        own, other = FakeProcess(1), FakeProcess(2)
        other.username = lambda: "another-user"
        with patch.object(monitor.psutil, "process_iter", return_value=iter([own, other])):
            records, eligible, _unused = self.service._sample_processes(1024**3)
        self.assertEqual(eligible, 1)
        self.assertEqual([row["pid"] for row in records], [1])
        self.assertNotIn("username", records[0])


class CrashRemovalTests(IsolatedLauncherTest):
    def test_retired_crash_module_and_generators_are_not_available(self):
        self.assertFalse((PROJECT_ROOT / "xvviix/services/crash_analysis.py").exists())
        self.assertFalse(hasattr(self.app, "create_game_crash_report"))
        self.assertFalse(hasattr(self.app, "analyze_process_exit"))

    def test_exit_codes_do_not_generate_crash_reports_or_guessed_causes(self):
        for kind in ("game", "app"):
            for code in (None, 0, 1, 0xC0000005, -1073741819):
                with self.subTest(kind=kind, code=code):
                    session = {
                        "item": {"name": "Synthetic app"},
                        "library_kind": kind,
                        "exit_code": code,
                        "started_at": 100,
                        "ended_at": 160,
                    }
                    with (
                        patch.object(self.app, "add_report") as report,
                        patch.object(self.app, "add_activity") as activity,
                    ):
                        self.app.finalize_process_session(123, session)
                        self.app.finalize_process_session(123, session)
                    report.assert_not_called()
                    activity.assert_called_once()
                    self.assertEqual(activity.call_args.args[0], "closed")
                    self.assertEqual(activity.call_args.kwargs["severity"], "info")
                    self.assertTrue(session["finalized"])

    def test_end_task_activity_and_playtime_session_closure_are_preserved(self):
        session = {
            "item": {"name": "Synthetic app"},
            "started_at": 100,
            "ended_at": 160,
            "end_requested": True,
        }
        with patch.object(self.app, "add_activity") as activity:
            self.app.finalize_process_session(12, session)
        self.assertEqual(activity.call_args.args[0], "end_task")

    def seed_reports(self):
        self.make_vault()
        old = self.app.models.normalize_report(
            {"id": "legacy", "kind": "game_crash", "title": "Retired fixture", "epoch": 100}
        )
        system = self.app.models.normalize_report(
            {
                "id": "system",
                "kind": "system_report",
                "title": "System fixture",
                "epoch": 200,
                "health_score": 90,
            }
        )
        self.assertTrue(self.app.vault._save(self.app.vault.REPORTS_FILE, [old, system]))
        self.app.load_all_data_files()
        return old, system

    def test_legacy_reports_are_hidden_but_not_rewritten_on_load(self):
        _old, system = self.seed_reports()
        before = Path(self.app.vault.REPORTS_FILE).read_bytes()
        self.app.load_all_data_files()
        self.assertEqual(self.app.system_report_items(), [system])
        self.assertEqual(len(self.app.reports), 2)
        self.assertEqual(Path(self.app.vault.REPORTS_FILE).read_bytes(), before)

    def test_new_crash_reports_are_rejected_without_writing(self):
        self.seed_reports()
        before = Path(self.app.vault.REPORTS_FILE).read_bytes()
        with self.assertRaises(ValueError):
            self.app.add_report({"kind": "game_crash", "title": "not supported"})
        self.assertEqual(Path(self.app.vault.REPORTS_FILE).read_bytes(), before)

    def test_system_report_creation_and_clearing_keep_retired_records(self):
        old, _system = self.seed_reports()
        self.app.add_report(
            {
                "id": "new-system",
                "kind": "system_report",
                "title": "New system fixture",
                "epoch": 300,
            }
        )
        self.assertEqual(len(self.app.system_report_items()), 2)
        self.assertIn(old, self.app.reports)
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=True),
            patch.object(self.app, "refresh"),
        ):
            self.assertTrue(self.app.clear_all_reports())
        self.assertEqual(self.app.reports, [old])
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.REPORTS_FILE), [old])

    def test_failed_system_report_clear_restores_the_in_memory_list(self):
        self.seed_reports()
        previous = list(self.app.reports)
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=True),
            patch.object(self.app.vault, "_save", return_value=False),
            patch.object(self.app, "refresh"),
        ):
            self.assertFalse(self.app.clear_all_reports())
        self.assertEqual(self.app.reports, previous)

    def test_legacy_crash_export_is_retired_but_system_export_still_works(self):
        old, system = self.seed_reports()
        with self.assertRaises(ValueError):
            self.app.format_report_text(old)
        self.assertIn("XVVIIX SYS REPORT", self.app.format_report_text(system))

    def test_overlay_opacity_is_validated_and_saved_as_a_preference(self):
        path = Path(self.app.SETTINGS_FILE)
        path.write_text(json.dumps({"monitor_overlay_opacity": 0.65}), encoding="utf-8")
        self.assertEqual(self.app.load_launcher_settings()["monitor_overlay_opacity"], 0.65)
        path.write_text(json.dumps({"monitor_overlay_opacity": 0}), encoding="utf-8")
        self.assertEqual(self.app.load_launcher_settings()["monitor_overlay_opacity"], 0.55)
        path.write_text(json.dumps({"monitor_overlay_opacity": float("nan")}), encoding="utf-8")
        self.assertEqual(self.app.load_launcher_settings()["monitor_overlay_opacity"], 0.80)
