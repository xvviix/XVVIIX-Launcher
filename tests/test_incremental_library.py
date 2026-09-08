"""Per-card identity, scroll anchors, mirrored pagers and grouped library actions."""

from unittest.mock import patch

from tests.support import IsolatedLauncherTest


class IncrementalLibraryUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.geometry("1100x700")
        self.app.root = self.root
        self.app.active_tab = "games"
        self.app.page_by_tab["games"] = 0
        self.app.current_scale = 1.0
        self.app.canvas = self.app.tk.Canvas(self.root, highlightthickness=0)
        self.app.canvas.pack(fill="both", expand=True)
        self.app.frame = self.app.tk.Frame(self.app.canvas)
        self.window_id = self.app.canvas.create_window((0, 0), window=self.app.frame, anchor="nw")
        self.app.frame.bind(
            "<Configure>",
            lambda _event: self.app.canvas.configure(scrollregion=self.app.canvas.bbox("all")),
        )
        self.app.canvas.bind(
            "<Configure>",
            lambda event: self.app.canvas.itemconfigure(self.window_id, width=event.width),
        )
        self.app.games = [
            self.app.models.normalize_item(
                {
                    "name": f"Program {index:03d}",
                    "path": rf"C:\TestFixtures\app{index}.exe",
                    "color": "#456789",
                    "playtime": index,
                }
            )
            for index in range(80)
        ]
        self.icon_patch = patch.object(self.app, "extract_icon_in_background")
        self.icon_patch.start()
        self.root.update()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.app.library_view is not None:
            self.app.library_view.dispose()
            self.app.library_view = None
        self.app.stop_card_artwork()
        self.app.cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.app.root = None
        self.icon_patch.stop()

    def pump(self, ms=80):
        self.root.after(ms, self.root.quit)
        self.root.mainloop()

    def render(self):
        self.app.display_items(self.app.games)
        self.pump(100)
        return self.app.library_view

    def test_playtime_update_preserves_all_widgets_and_scroll_position(self):
        view = self.render()
        self.app.canvas.yview_moveto(0.4)
        self.pump(40)
        old_scroll = self.app.canvas.canvasy(0)
        widgets = {key: handle.widget for key, handle in view.cards.items()}
        self.app.games[15]["playtime"] += 90
        with patch.object(view, "build_card", wraps=view.build_card) as build:
            self.app.display_items(self.app.games)
            self.pump(80)
            build.assert_not_called()
        self.assertEqual({key: handle.widget for key, handle in view.cards.items()}, widgets)
        self.assertAlmostEqual(self.app.canvas.canvasy(0), old_scroll, delta=2)

    def test_structural_change_rebuilds_only_that_card_and_preserves_anchor(self):
        view = self.render()
        self.app.canvas.yview_moveto(0.42)
        self.pump(40)
        anchor = view.capture_anchor()
        widgets = {key: handle.widget for key, handle in view.cards.items()}
        changed = self.app.games[0]
        changed["name"] = "A deliberately long title " * 12
        self.app.display_items(self.app.games)
        self.pump(100)
        self.assertIsNot(view.cards[id(changed)].widget, widgets[id(changed)])
        for key, widget in widgets.items():
            if key != id(changed):
                self.assertIs(view.cards[key].widget, widget)
        current = view.capture_anchor()
        self.assertEqual(current[0], anchor[0])
        self.assertAlmostEqual(current[1], anchor[1], delta=3)

    def test_top_and_bottom_pagers_have_matching_states_and_callbacks(self):
        with patch.object(self.app, "change_page") as page:
            view = self.render()
            self.assertEqual(view.top_pager["previous"].cget("state"), "disabled")
            self.assertEqual(view.bottom["previous"].cget("state"), "disabled")
            view.top_pager["next"].invoke()
            view.bottom["next"].invoke()
            self.assertEqual([call.args for call in page.call_args_list], [(1,), (1,)])
        self.app.page_by_tab["games"] = 2
        self.app.display_items(self.app.games)
        self.pump()
        self.assertEqual(view.top_pager["next"].cget("state"), "disabled")
        self.assertEqual(view.bottom["next"].cget("state"), "disabled")

    def test_multi_selection_survives_card_updates_and_page_changes(self):
        view = self.render()
        first, second = self.app.games[:2]
        view.toggle(first)
        view.toggle(second)
        self.app.games[0]["playtime"] += 10
        self.app.display_items(self.app.games)
        self.pump()
        self.assertEqual({id(item) for item in view.selected_items()}, {id(first), id(second)})
        self.app.page_by_tab["games"] = 1
        self.app.display_items(self.app.games)
        self.pump()
        self.assertEqual(len(view.selected_items()), 2)
        self.assertIn("OFF PAGE", view.selection_label.cget("text"))
        view.clear_selection()
        self.assertEqual(view.selected_items(), [])


class BulkLibraryActionTests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        self.make_vault()
        self.app.load_all_data_files()
        self.app.games.extend(
            self.app.models.normalize_item(
                {"name": f"Fixture {index}", "path": rf"C:\Fixture\{index}.exe", "color": "#123456"}
            )
            for index in range(3)
        )
        self.app.vault._save(self.app.vault.GAMES_FILE, self.app.games)

    def test_move_selected_entries_without_losing_duplicates(self):
        selected = self.app.games[1:3]
        duplicate = dict(selected[0])
        self.app.apps.append(duplicate)
        self.app.vault._save(self.app.vault.APPS_FILE, self.app.apps)
        result = self.app._perform_library_action(selected, "games", "move", "apps")
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertIn(selected[0], self.app.games)
        self.assertIn(selected[1], self.app.apps)
        self.assertEqual(len(self.app.vault.read_data_json(self.app.vault.APPS_FILE)), 2)

    def test_remove_is_library_only_and_confirmation_can_cancel(self):
        selected = list(self.app.games[1:3])
        before = self.data_bytes()
        with patch.object(self.app.messagebox, "askyesno", return_value=False):
            result = self.app._perform_library_action(selected, "games", "delete")
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.data_bytes(), before)
        with patch.object(self.app.messagebox, "askyesno", return_value=True) as confirm:
            result = self.app._perform_library_action(selected, "games", "delete")
        self.assertEqual(result["changed"], 2)
        self.assertIn("not deleted or uninstalled", confirm.call_args.args[1])
        self.assertTrue(all(item not in self.app.games for item in selected))

    def test_pin_selected_and_preserve_other_entries(self):
        selected = self.app.games[1:3]
        self.app._perform_library_action(selected, "games", "pin", True)
        self.assertTrue(all(item["pinned"] for item in selected))
        self.assertFalse(self.app.games[0]["pinned"])

    def test_failed_move_restores_model_and_original_encrypted_files(self):
        selected = list(self.app.games[1:3])
        before = self.data_bytes()
        original = self.app.vault._save

        def fail_source(path, data):
            if path == self.app.vault.GAMES_FILE:
                return False
            return original(path, data)

        with (
            patch.object(self.app.vault, "_save", side_effect=fail_source),
            patch.object(self.app.messagebox, "showerror"),
        ):
            result = self.app._perform_library_action(selected, "games", "move", "apps")
        self.assertTrue(result["failed"])
        self.assertEqual(self.data_bytes(), before)
        self.assertTrue(all(item in self.app.games for item in selected))
        self.assertEqual(self.app.apps, [])
