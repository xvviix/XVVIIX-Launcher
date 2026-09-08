"""Regression cases for runtime spam and embedded/duplicate 7-Zip executables."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.support import IsolatedLauncherTest

from xvviix.services import discovery, software_catalog as catalog


class SoftwareCatalogTests(unittest.TestCase):
    def classify(self, path, name, **fields):
        return discovery.classify_item(
            {"path": path, "name": name, **fields}, metadata_reader=lambda _path: {}
        )

    def test_net_runtimes_and_command_line_helpers_are_not_apps(self):
        for name, path in (
            ("Microsoft .NET Runtime 8.0.10", r"C:\Program Files\dotnet\dotnet.exe"),
            ("Microsoft ASP.NET Core Runtime", r"C:\dotnet\host.exe"),
            ("Microsoft Visual C++ 2015-2022 Redistributable", r"C:\setup\runtime.exe"),
            ("Microsoft Edge WebView2 Runtime", r"C:\runtime\webview.exe"),
            ("7-Zip", r"C:\Program Files\AnotherApp\resources\7za.exe"),
        ):
            with self.subTest(name=name):
                self.assertTrue(catalog.is_runtime_component(name, path))
                self.assertEqual(self.classify(path, name, source="registry")["kind"], "ignore")

    def test_real_seven_zip_gui_is_not_the_same_as_an_embedded_helper(self):
        real = {
            "path": r"C:\Program Files\7-Zip\7zFM.exe",
            "name": "7-Zip",
            "source": "registry",
            "install_location": r"C:\Program Files\7-Zip",
        }
        embedded = {
            "path": r"C:\Program Files\OtherApp\tools\7zFM.exe",
            "name": "7-Zip",
            "source": "filesystem",
            "scan_root": r"C:\Program Files",
        }
        self.assertFalse(catalog.embedded_seven_zip(real))
        self.assertTrue(catalog.embedded_seven_zip(embedded))
        self.assertEqual(
            self.classify(
                embedded["path"],
                embedded["name"],
                source="filesystem",
                scan_root=embedded["scan_root"],
            )["kind"],
            "ignore",
        )

    def test_duplicate_product_versions_prefer_registered_main_launcher(self):
        base = {
            "name": "Example Browser",
            "kind": "app",
            "identity": {"product": "Example Browser", "publisher": "Example Inc"},
            "scan_root": r"C:\Program Files",
        }
        old = {
            **base,
            "path": r"C:\Program Files\Example\Application\100.0\browser.exe",
            "source": "filesystem",
        }
        new = {
            **base,
            "path": r"C:\Program Files\Example\Application\101.0\browser.exe",
            "source": "filesystem",
        }
        main = {
            **base,
            "path": r"C:\Program Files\Example\Application\browser.exe",
            "source": "registry",
        }
        result = catalog.deduplicate_applications([old, new, main])
        self.assertEqual(result, [main])

    def test_different_products_in_one_vendor_folder_are_not_merged(self):
        rows = [
            {
                "name": name,
                "path": path,
                "kind": "app",
                "identity": {"product": name, "publisher": "Vendor"},
                "scan_root": r"C:\Program Files",
            }
            for name, path in (
                ("Writer", r"C:\Program Files\Vendor\Writer\writer.exe"),
                ("Spreadsheet", r"C:\Program Files\Vendor\Sheet\sheet.exe"),
            )
        ]
        self.assertEqual(len(catalog.deduplicate_applications(rows)), 2)

    def test_known_locations_do_not_expand_to_whole_drives(self):
        with tempfile.TemporaryDirectory(prefix="xvviix-known-") as directory:
            root = Path(directory)
            programs = root / "Program Files"
            programs.mkdir()
            child = programs / "OneApp"
            child.mkdir()
            local = root / "Local"
            (local / "Programs").mkdir(parents=True)
            result = catalog.known_installation_roots(
                [
                    {
                        "name": "OneApp",
                        "path": str(child / "app.exe"),
                        "install_location": str(child),
                    }
                ],
                environ={"ProgramFiles": str(programs), "LOCALAPPDATA": str(local)},
            )
            self.assertEqual(set(result), {str(programs), str(local / "Programs")})
            self.assertNotIn(directory, result)
            self.assertEqual(discovery.scan_modes(), ("registered", "search"))


class ScanModePolicyTests(IsolatedLauncherTest):
    def test_control_panel_mode_never_walks_folders_or_start_menu(self):
        self.app.scan_cancel = False
        self.app.scan_scope = "registered"
        with (
            patch.object(self.app, "HAS_WIN32COM", False),
            patch.object(self.app, "scan_registry", return_value=[]) as registry,
            patch.object(self.app, "scan_start_menu") as menu,
            patch.object(self.app.discovery, "iter_executables") as walker,
            patch.object(
                self.app,
                "apply_intelligent_scan_results",
                return_value={"game": 0, "app": 0, "unknown": 0},
            ) as apply,
            patch.object(self.app, "add_activity"),
        ):
            self.app.scan_all_programs("registered")
        registry.assert_called_once_with(include_locations=False)
        menu.assert_not_called()
        walker.assert_not_called()
        apply.assert_called_once()

    def test_search_mode_uses_only_known_installation_roots(self):
        self.app.scan_cancel = False
        self.app.scan_scope = "search"
        with (
            patch.object(self.app, "HAS_WIN32COM", False),
            patch.object(self.app, "scan_registry", return_value=[]),
            patch.object(self.app, "scan_start_menu", return_value=[]),
            patch.object(
                self.app.software_catalog,
                "known_installation_roots",
                return_value=[r"C:\Program Files"],
            ) as roots,
            patch.object(self.app.discovery, "is_local_scan_path", return_value=True),
            patch.object(self.app.discovery, "iter_executables", return_value=iter([])) as walker,
            patch.object(
                self.app,
                "apply_intelligent_scan_results",
                return_value={"game": 0, "app": 0, "unknown": 0},
            ),
            patch.object(self.app, "add_activity"),
        ):
            self.app.scan_all_programs("search")
        roots.assert_called_once()
        self.assertEqual(walker.call_args.args[0], [r"C:\Program Files"])
