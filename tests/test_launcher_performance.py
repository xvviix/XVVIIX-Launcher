"""Integration checks for overlay ownership and low-work main-window updates."""

import unittest
from unittest.mock import Mock, patch

from tests.support import IsolatedLauncherTest
from tests.test_monitor_overlay_ui import sample_snapshot


class LauncherIdleTests(IsolatedLauncherTest):
    def test_empty_library_does_not_enumerate_all_processes(self):
        app = self.app
        app.games, app.apps = [], []
        with (
            patch.object(app.monitor_stop, "wait", side_effect=[False, True]),
            patch.object(app.psutil, "process_iter") as scan,
            patch.object(app, "account_tracked_processes") as account,
        ):
            app.monitor_running_apps()
        scan.assert_not_called()
        account.assert_called_once()


class LauncherPerformanceUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app.root = self.root
        self.app.active_tab = "games"
        self.app.monitor_stop.clear()
        self.app.monitor_overlay_ui.clear()
        self.app.monitor_overlay = None
        self.app.hardware_monitor_state = "ready"
        self.service = Mock()
        self.service.is_running.return_value = True
        self.service.overlay_snapshot.side_effect = sample_snapshot
        self.app.hardware_monitor = self.service
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        self.app.close_hardware_overlay()
        self.app.cancel_hardware_monitor_idle_stop()
        self.app.cancel_hardware_monitor_view_refresh()
        self.app.cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.app.root = None
        self.app.hardware_monitor = None
        self.app.monitor_ui.clear()

    def pump(self, ms):
        self.root.after(ms, self.root.quit)
        self.root.mainloop()

    def test_overlay_uses_light_snapshots_and_reopening_reuses_the_window(self):
        app = self.app
        app.open_hardware_overlay()
        self.pump(100)
        view = app.monitor_overlay_ui["view"]
        self.assertTrue(view.visible)
        app.open_hardware_overlay()
        self.assertIs(app.monitor_overlay_ui["view"], view)
        self.service.overlay_snapshot.assert_called()
        self.service.snapshot.assert_not_called()
        app.close_hardware_overlay()
        self.assertTrue(view.closed)
        self.assertFalse(view.frame_clock.running)
        self.assertIsNone(app.monitor_overlay)

    def test_overlay_window_appears_while_sensors_are_still_starting(self):
        app = self.app
        app.hardware_monitor = None
        app.hardware_monitor_state = "starting"
        with patch.object(app, "request_hardware_monitor_start", return_value=False) as start:
            app.open_hardware_overlay()
            self.pump(70)
            self.assertTrue(app.widget_exists(app.monitor_overlay))
            view = app.monitor_overlay_ui["view"]
            self.assertIn("WAITING", view.canvas.itemcget("status", "text"))
            start.assert_called()
            app._finish_hardware_monitor_start(self.service)
            self.pump(70)
            self.assertTrue(view.frame_clock.running)

    def test_process_panels_do_not_create_more_than_five_rows_each(self):
        panels = self.app.TopProcessPanels(self.root)
        records = [
            {
                "pid": index + 1,
                "name": f"fixture-{index}.exe",
                "cpu_percent": 10,
                "memory_percent": 1,
                "memory_bytes": 1024,
            }
            for index in range(100)
        ]
        panels.update(records, records, 100)
        self.assertEqual(len(panels.trees["cpu"].get_children()), 5)
        self.assertEqual(len(panels.trees["memory"].get_children()), 5)
        self.assertEqual(int(panels.trees["cpu"].cget("height")), 5)

    def test_unmapped_monitor_chart_accepts_tk_screen_distance_units(self):
        chart = self.app.tk.Canvas(self.root, width="10c", height="1c")
        self.app._draw_monitor_history(chart, [10, 20, 15], "#22d3ee")
        self.assertEqual(len(chart.find_all()), 3)
        self.assertGreater(chart.coords(chart._monitor_history_items[0])[2], 40)

    def test_full_monitor_uses_only_two_top_five_panels(self):
        app = self.app
        self.root.geometry("1100x850")
        self.root.deiconify()
        self.root.update()
        app.active_tab = "monitor"
        app.frame = app.tk.Frame(self.root)
        app.frame.pack(fill="both", expand=True)
        app.canvas = app.tk.Canvas(self.root)
        snapshot = sample_snapshot()
        snapshot["top_cpu"] = [
            {
                "pid": i + 1,
                "name": f"fixture-{i}.exe",
                "cpu_percent": 20 - i,
                "memory_percent": 1,
                "memory_bytes": 1024,
            }
            for i in range(9)
        ]
        snapshot["top_memory"] = list(reversed(snapshot["top_cpu"]))
        snapshot["user_process_total"] = 90
        snapshot["process_revision"] = 1
        self.service.snapshot.return_value = snapshot
        app.display_hardware_monitor()
        panels = app.monitor_ui["top_panels"]
        self.assertEqual(len(panels.trees["cpu"].get_children()), 5)
        self.assertEqual(len(panels.trees["memory"].get_children()), 5)
        self.assertNotIn("process_tree", app.monitor_ui)
        self.assertNotIn("process_detail", app.monitor_ui)

    def test_reports_view_has_no_crash_filter_or_retired_report_cards(self):
        app = self.app
        app.frame = app.tk.Frame(self.root)
        app.frame.pack(fill="both", expand=True)
        app.canvas = app.tk.Canvas(self.root)
        app.selected_report_id = None
        app.reports = [
            app.models.normalize_report(
                {"id": "legacy", "kind": "game_crash", "title": "SHOULD_NOT_BE_VISIBLE"}
            ),
            app.models.normalize_report(
                {
                    "id": "sys",
                    "kind": "system_report",
                    "title": "Visible system diagnostic",
                    "health_score": 90,
                }
            ),
        ]
        app.display_reports()

        def texts(widget):
            result = []
            for child in widget.winfo_children():
                try:
                    result.append(str(child.cget("text")))
                except app.tk.TclError:
                    pass
                result.extend(texts(child))
            return result

        text = "\n".join(texts(app.frame))
        self.assertIn("Visible system diagnostic", text)
        self.assertNotIn("CRASH", text.upper())
        self.assertNotIn("SHOULD_NOT_BE_VISIBLE", text)
        self.assertEqual(len(app.reports), 2)

    def test_unchanged_process_rows_are_not_reconfigured_or_moved(self):
        app = self.app
        panels = app.TopProcessPanels(self.root)
        tree = panels.trees["cpu"]
        records = [
            {
                "pid": 1,
                "name": "first.exe",
                "cpu_percent": 10,
                "memory_bytes": 100,
                "memory_percent": 1,
            },
            {
                "pid": 2,
                "name": "second.exe",
                "cpu_percent": 5,
                "memory_bytes": 200,
                "memory_percent": 2,
            },
        ]
        panels.update(records, list(reversed(records)), 2)
        with (
            patch.object(tree, "item", wraps=tree.item) as configure,
            patch.object(tree, "move", wraps=tree.move) as move,
        ):
            panels.update(records, list(reversed(records)), 2)
            configure.assert_not_called()
            move.assert_not_called()
            records[1] = {**records[1], "cpu_percent": 30}
            panels.update(list(reversed(records)), list(reversed(records)), 2)
            self.assertGreater(move.call_count, 0)
        self.assertEqual(tree.get_children(), ("cpu-2", "cpu-1"))


if __name__ == "__main__":
    unittest.main()
