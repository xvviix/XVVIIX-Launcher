"""Local-drive traversal and conservative rule classification, using disposable trees."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from xvviix.services import discovery
from tests.support import IsolatedLauncherTest


class DiscoveryTraversalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix-scan-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def exe(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic executable, not runnable")
        return path

    def test_only_registered_and_known_location_modes_are_available(self):
        self.assertEqual(discovery.scan_modes(), ("registered", "search"))
        self.assertFalse(hasattr(discovery, "local_drive_roots"))

    def test_unc_paths_do_not_turn_registered_shortcuts_into_network_scans(self):
        self.assertFalse(discovery.is_local_scan_path(r"\\server\share\app.exe"))
        self.assertFalse(discovery.is_local_scan_path(r"\\?\UNC\server\share\app.exe"))
        with patch.object(discovery, "_local_drive_type", return_value=True):
            self.assertTrue(discovery.is_local_scan_path(r"C:\Apps\app.exe"))

    def test_recursive_scan_covers_both_selected_drives_and_deep_folders(self):
        first = self.exe("drive_c/Portable/Deep/Folder/Game.exe")
        second = self.exe("drive_e/Games/Other/Binaries/Win64/Other.exe")
        self.exe("drive_e/Games/Other/setup.exe")
        stats = discovery.ScanStats()
        before = {p: p.read_bytes() for p in self.root.rglob("*.exe")}
        result = list(
            discovery.iter_executables([self.root / "drive_c", self.root / "drive_e"], stats=stats)
        )
        self.assertEqual({row["path"] for row in result}, {str(first), str(second)})
        self.assertEqual(stats.roots_done, 2)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*.exe")})

    def test_system_cache_and_reparse_paths_are_not_followed(self):
        valid = self.exe("root/Tools/tool.exe")
        self.exe("root/Windows/System32/helper.exe")
        self.exe("root/project/node_modules/dependency/program.exe")
        outside = self.exe("outside/outside.exe")
        try:
            (self.root / "root/linked").symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            pass
        stats = discovery.ScanStats()
        result = list(discovery.iter_executables([self.root / "root"], stats=stats))
        self.assertEqual([row["path"] for row in result], [str(valid)])
        self.assertGreaterEqual(stats.skipped_system, 2)

    def test_cancellation_stops_without_modifying_files(self):
        self.exe("root/one.exe")
        self.exe("root/two.exe")
        cancelled = [False]
        generator = discovery.iter_executables([self.root / "root"], cancelled=lambda: cancelled[0])
        next(generator)
        cancelled[0] = True
        with self.assertRaises(discovery.ScanCancelled):
            next(generator)
        self.assertEqual(len(list((self.root / "root").glob("*.exe"))), 2)

    def test_permission_failures_and_safety_limit_are_reported(self):
        self.exe("root/one.exe")
        self.exe("root/two.exe")
        self.exe("root/three.exe")
        stats = discovery.ScanStats()
        result = list(
            discovery.iter_executables([self.root / "root"], stats=stats, candidate_limit=2)
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(stats.limit_reached)
        stats = discovery.ScanStats()
        with patch.object(discovery.os, "scandir", side_effect=PermissionError("fixture")):
            self.assertEqual(list(discovery.iter_executables([self.root], stats=stats)), [])
        self.assertEqual(stats.inaccessible, 1)


class ClassificationTests(unittest.TestCase):
    def classify(self, path, **fields):
        item = {"name": Path(path).stem, "path": path, **fields}
        return discovery.classify_item(item, metadata_reader=lambda _path: {})

    def test_game_platform_client_in_games_directory_stays_an_app(self):
        for name in ("Steam.exe", "EpicGamesLauncher.exe", "UnityHub.exe", "UnrealEditor.exe"):
            self.assertEqual(self.classify("D:\\Games\\Tools\\" + name)["kind"], "app")

    def test_publisher_alone_does_not_turn_every_binary_into_a_game(self):
        result = self.classify(
            r"D:\Portable\utility.exe", publisher="Ubisoft", product="Account utility"
        )
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("publisher alone", result["identity"]["classification_reason"])

    def test_program_files_path_alone_is_not_sufficient(self):
        self.assertEqual(self.classify(r"C:\Program Files\Unknown\worker.exe")["kind"], "unknown")

    def test_registered_user_program_has_stronger_application_evidence(self):
        result = self.classify(
            r"C:\Program Files\Example\example.exe", source="registry", publisher="Example Software"
        )
        self.assertEqual(result["kind"], "app")
        self.assertGreaterEqual(result["confidence"], 0.9)

    def test_game_library_path_and_helpers_are_distinguished(self):
        self.assertEqual(
            self.classify(r"E:\SteamLibrary\steamapps\common\Example\game.exe")["kind"], "game"
        )
        for name in (
            "unins000.exe",
            "setup-game-1.0.exe",
            "UnityCrashHandler64.exe",
            "steamwebhelper.exe",
        ):
            self.assertEqual(self.classify("E:\\Games\\Example\\" + name)["kind"], "ignore")

    def test_unity_game_requires_matching_game_data(self):
        with tempfile.TemporaryDirectory(prefix="xvviix-engine-") as directory:
            root = Path(directory)
            (root / "Example.exe").write_bytes(b"synthetic")
            (root / "UnityPlayer.dll").write_bytes(b"synthetic")
            self.assertEqual(self.classify(str(root / "Example.exe"))["kind"], "unknown")
            (root / "Example_Data").mkdir()
            self.assertEqual(self.classify(str(root / "Example.exe"))["kind"], "game")

    def test_installer_description_is_filtered_even_with_a_custom_filename(self):
        result = self.classify(
            r"C:\Downloads\product-2026.exe", description="Product Installer", source="start_menu"
        )
        self.assertEqual(result["kind"], "ignore")

    def test_unknown_files_are_retained_for_review_not_executed(self):
        reader = Mock(return_value={})
        result = discovery.classify_item(
            {"name": "Portable fixture", "path": "D:\\Portable\\fixture.exe"},
            metadata_reader=reader,
        )
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("manual review", result["identity"]["classification_reason"])


class ScanApplicationTests(IsolatedLauncherTest):
    def test_primary_drive_filter_rejects_other_drives(self):
        self.assertTrue(self.app._path_in_scan_roots(r"C:\Apps\one.exe", ["C:\\"]))
        self.assertFalse(self.app._path_in_scan_roots(r"D:\Apps\one.exe", ["C:\\"]))

    def test_known_library_entries_and_ambiguous_items_are_not_silently_deleted(self):
        self.make_vault()
        self.app.load_all_data_files()
        existing = self.app.games[0]
        self.app.scan_cancel = False
        self.app.founded = [
            self.app.models.normalize_item(
                {"name": "Manually kept", "path": r"C:\Tools\kept.exe", "color": "#123456"}
            )
        ]
        before = dict(existing)
        result = self.app.apply_intelligent_scan_results(
            [
                {
                    "name": "Existing renamed guess",
                    "path": existing["path"],
                    "kind": "app",
                    "confidence": 0.99,
                },
                {
                    "name": "New unknown",
                    "path": r"D:\Tools\unknown.exe",
                    "kind": "unknown",
                    "confidence": 0.45,
                },
                {
                    "name": "New game",
                    "path": r"D:\Games\game.exe",
                    "kind": "game",
                    "confidence": 0.95,
                },
            ]
        )
        self.assertEqual(existing, before)
        self.assertIn(existing, self.app.games)
        self.assertEqual(len(self.app.founded), 2)
        self.assertEqual(result["game"], 1)
        self.assertEqual(result["unknown"], 1)

    def test_failed_scan_save_is_kept_dirty_for_retry_on_exit(self):
        self.make_vault()
        self.app.load_all_data_files()
        self.app.scan_cancel = False
        with patch.object(self.app.vault, "_save", return_value=False):
            with self.assertRaises(OSError):
                self.app.apply_intelligent_scan_results(
                    [
                        {
                            "name": "New fixture",
                            "path": r"D:\Games\new.exe",
                            "kind": "game",
                            "confidence": 0.95,
                        }
                    ]
                )
            self.assertIn("games", self.app.dirty_scan_libraries)
            self.assertTrue(self.app._checkpoint_before_exit())

    def test_cancel_before_commit_leaves_library_bytes_unchanged(self):
        self.make_vault()
        before = self.data_bytes()
        self.app.scan_cancel = True
        with self.assertRaises(self.app.discovery.ScanCancelled):
            self.app.apply_intelligent_scan_results([])
        self.assertEqual(self.data_bytes(), before)
        self.app.scan_cancel = False


class ScanScopeUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app.root = self.root
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.app.root = None

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def choose(self, mode):
        def drive():
            widgets = {widget.winfo_name(): widget for widget in self.widgets(self.root)}
            widgets["scope_" + mode].invoke()
            widgets["start_scope"].invoke()

        self.root.after(40, drive)
        return self.app.choose_scan_scope(self.root, "registered")

    def test_user_can_choose_registered_or_known_installation_locations(self):
        self.assertEqual(self.choose("registered"), "registered")
        self.assertEqual(self.choose("search"), "search")

    def test_cancelling_scope_selection_does_not_start_a_scan(self):
        def cancel():
            next(
                widget
                for widget in self.widgets(self.root)
                if widget.winfo_name() == "cancel_scope"
            ).invoke()

        self.root.after(40, cancel)
        self.assertIsNone(self.app.choose_scan_scope(self.root))

    def test_hiding_cancelled_scan_releases_modal_grab(self):
        self.app.create_scan_window()
        self.assertIsNotNone(self.root.grab_current())
        self.app.request_scan_cancel(close_window=True)
        self.assertIsNone(self.root.grab_current())
        self.app.scan_window.destroy()
        self.app.scan_cancel = False
