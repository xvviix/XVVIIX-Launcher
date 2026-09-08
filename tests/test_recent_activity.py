"""Useful activity history without card-list resets or destructive cleanup."""

import time
import tkinter as tk
import unittest
from unittest.mock import patch

from tests.support import IsolatedLauncherTest
from xvviix.ui.activity_view import ActivityView, activity_age
from xvviix.ui.lifecycle import cancel_pending_callbacks


class ActivityModelTests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        self.make_vault()
        self.app.load_all_data_files()

    def test_identical_event_bursts_are_not_duplicated(self):
        first = self.app.add_activity("library", "Pinned entries", detail="3 items", epoch=100)
        second = self.app.add_activity("library", "Pinned entries", detail="3 items", epoch=102)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.app.recent_activity), 1)
        self.app.add_activity("library", "Pinned entries", detail="4 items", epoch=102)
        self.assertEqual(len(self.app.recent_activity), 2)

    def test_clear_history_keeps_libraries_and_playtime(self):
        self.app.add_activity("launch", "Launched fixture")
        before = self.app.vault.read_data_json(self.app.vault.GAMES_FILE)
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=True),
            patch.object(self.app, "update_activity_rail"),
        ):
            self.assertTrue(self.app.clear_recent_activity())
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.ACTIVITY_FILE), [])
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.GAMES_FILE), before)

    def test_failed_clear_restores_activity_in_memory(self):
        self.app.add_activity("launch", "Launched fixture")
        before = list(self.app.recent_activity)
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=True),
            patch.object(self.app.vault, "_save", return_value=False),
        ):
            self.assertFalse(self.app.clear_recent_activity())
        self.assertEqual(self.app.recent_activity, before)

    def test_age_text_distinguishes_seconds_minutes_and_hours(self):
        self.assertEqual(activity_age(100, now=102), "JUST NOW")
        self.assertEqual(activity_age(100, now=130), "30s ago")
        self.assertEqual(activity_age(100, now=280), "3m ago")
        self.assertEqual(activity_age(100, now=7300), "2h ago")


class ActivityViewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.geometry("400x200+10+10")
        self.root.update()
        now = time.time()
        self.rows = [
            {
                "id": str(index),
                "kind": "library" if index % 2 else "launch",
                "title": f"Event {index}",
                "item_name": f"Program {index}",
                "epoch": now - index * 60,
                "detail": f"Detail {index}",
                "severity": "info",
            }
            for index in range(60)
        ]
        self.view = ActivityView(self.root, lambda: list(self.rows), lambda: True)
        self.pump(50)

    def tearDown(self):
        self.view.destroy()
        cancel_pending_callbacks(self.root)
        self.root.destroy()

    def pump(self, ms):
        self.root.after(ms, self.root.quit)
        self.root.mainloop()

    def test_search_and_event_filter_are_live(self):
        self.view.query.set("Program 12")
        self.assertEqual(self.view.tree.get_children(), ("12",))
        self.view.query.set("")
        self.view.category.set("library")
        self.assertTrue(all(int(key) % 2 for key in self.view.tree.get_children()))

    def test_new_events_preserve_selection_when_browsing_history(self):
        self.view.tree.selection_set("30")
        self.view.tree.yview_moveto(0.5)
        self.pump(30)
        before = self.view.tree.identify_row(40)
        self.rows.insert(
            0,
            {
                "id": "new",
                "kind": "launch",
                "title": "New event",
                "epoch": time.time(),
                "item_name": "New program",
            },
        )
        self.view.refresh()
        self.pump(30)
        self.assertEqual(self.view.tree.selection(), ("30",))
        self.assertEqual(self.view.tree.identify_row(40), before)

    def test_history_updates_do_not_touch_any_library_renderer(self):
        before = self.view.tree.get_children()
        self.rows[0] = {**self.rows[0], "detail": "Updated result"}
        self.view.refresh()
        self.assertEqual(self.view.tree.get_children(), before)
        self.assertEqual(self.view._records["0"]["detail"], "Updated result")

    def test_close_cancels_refresh_job(self):
        self.assertIsNotNone(self.view.job)
        self.view.destroy()
        self.assertTrue(self.view.closed)
        self.assertIsNone(self.view.job)
