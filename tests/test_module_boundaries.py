"""Architecture and independent-state checks for the extracted package."""

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from xvviix import paths
from xvviix.storage.vault import VaultStore
from tests.support import PROJECT_ROOT, PASSWORD, NEW_PASSWORD


class ModuleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix-boundary-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def subprocess_python(self, code, cwd=None):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=cwd or self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_backend_imports_do_not_start_app_tk_audio_or_workers(self):
        code = textwrap.dedent(f"""
            import sys, threading
            from pathlib import Path
            sys.dont_write_bytecode = True
            sys.path.insert(0, {str(PROJECT_ROOT)!r})
            before = set(Path.cwd().iterdir())
            import xvviix
            from xvviix.storage.vault import VaultStore
            from xvviix.security import crypto
            from xvviix.services import audio, hardware_monitor
            from xvviix import models, utils, paths
            VaultStore(Path.cwd() / 'not-created-yet')
            assert 'xvviix.app' not in sys.modules
            assert 'tkinter' not in sys.modules
            assert 'pygame' not in sys.modules
            assert 'pynvml' not in sys.modules
            assert len(threading.enumerate()) == 1
            assert set(Path.cwd().iterdir()) == before
            print('headless imports OK')
        """)
        result = self.subprocess_python(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "headless imports OK")

    def test_data_and_service_modules_have_no_gui_or_application_imports(self):
        candidates = [
            PROJECT_ROOT / "xvviix" / name
            for name in ("models.py", "utils.py", "constants.py", "paths.py")
        ]
        for directory in ("security", "storage", "services"):
            candidates.extend((PROJECT_ROOT / "xvviix" / directory).glob("*.py"))
        for path in candidates:
            with self.subTest(module=path.name):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        imported = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imported = [node.module or ""]
                        if not node.module:
                            imported.extend(item.name for item in node.names)
                    else:
                        continue
                    for name in imported:
                        self.assertNotIn(
                            name.split(".")[0], {"tkinter", "app", "ui", "game_launcher"}
                        )
                        self.assertNotIn(name, {"xvviix.app", "xvviix.ui"})

    def test_entrypoint_import_is_inert(self):
        code = textwrap.dedent(f"""
            import importlib.util, sys
            from pathlib import Path
            sys.dont_write_bytecode = True
            before = set(Path.cwd().iterdir())
            spec = importlib.util.spec_from_file_location('launcher_entry_test', {str(PROJECT_ROOT / "game_launcher.py")!r})
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert callable(module.run)
            assert 'xvviix.app' not in sys.modules
            assert 'tkinter' not in sys.modules
            assert set(Path.cwd().iterdir()) == before
        """)
        result = self.subprocess_python(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_entrypoint_reports_an_incomplete_copy(self):
        shutil.copy2(PROJECT_ROOT / "game_launcher.py", self.root / "game_launcher.py")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(self.root / "game_launcher.py")],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("installation is incomplete", result.stderr)
        self.assertIn("xvviix folder", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_incomplete_copy_does_not_run_another_package_from_pythonpath(self):
        shutil.copy2(PROJECT_ROOT / "game_launcher.py", self.root / "game_launcher.py")
        external = self.root / "external"
        (external / "xvviix").mkdir(parents=True)
        (external / "xvviix/__init__.py").write_text("", encoding="utf-8")
        (external / "xvviix/app.py").write_text("def run():\n    return 23\n", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(external)
        result = subprocess.run(
            [sys.executable, str(self.root / "game_launcher.py")],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("installation is incomplete", result.stderr)

    def test_entrypoint_delegates_and_preserves_exit_code_from_another_cwd(self):
        install = self.root / "install with spaces"
        install.mkdir()
        (install / "xvviix").mkdir()
        shutil.copy2(PROJECT_ROOT / "game_launcher.py", install / "game_launcher.py")
        (install / "xvviix/__init__.py").write_text("", encoding="utf-8")
        (install / "xvviix/app.py").write_text("def run():\n    return 23\n", encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(install / "game_launcher.py")],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)

    def test_source_resources_stay_at_project_root_not_package_directory(self):
        self.assertEqual(Path(paths.install_directory()).resolve(), PROJECT_ROOT)
        self.assertEqual(
            Path(paths.resource_path("assets/xvviix_header.png")).resolve(),
            PROJECT_ROOT / "assets/xvviix_header.png",
        )

    def test_frozen_install_and_resource_locations_remain_distinct(self):
        install = self.root / "installed"
        bundled = self.root / "unpacked-resources"
        with (
            patch.object(paths.sys, "frozen", True, create=True),
            patch.object(paths.sys, "_MEIPASS", str(bundled), create=True),
            patch.object(paths.sys, "executable", str(install / "launcher.exe")),
        ):
            self.assertEqual(Path(paths.install_directory()), install)
            self.assertEqual(Path(paths.resource_path("icon.ico")), bundled / "icon.ico")

    def test_unwritable_install_uses_existing_per_user_fallback_policy(self):
        protected = self.root / "protected-install"
        user_root = self.root / "local-app-data"
        real_makedirs = os.makedirs

        def mkdir(path, *args, **kwargs):
            if os.fspath(path) == str(protected):
                raise PermissionError("synthetic read-only install")
            return real_makedirs(path, *args, **kwargs)

        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(user_root)}),
            patch.object(paths.os, "makedirs", side_effect=mkdir),
        ):
            selected = paths.choose_data_dir(protected)
        self.assertEqual(Path(selected), user_root / "XVVIIXLauncher")
        self.assertTrue(Path(selected).is_dir())

    def test_two_vault_instances_do_not_share_keys_files_or_warnings(self):
        first = VaultStore(self.root / "first")
        second = VaultStore(self.root / "second")
        first.create_data_vault(PASSWORD)
        second.create_data_vault(NEW_PASSWORD)
        self.assertNotEqual(first.vault_key, second.vault_key)
        self.assertNotEqual(first.GAMES_FILE, second.GAMES_FILE)
        self.assertIsNot(first.data_lock, second.data_lock)
        self.assertIsNot(first.load_warnings, second.load_warnings)
        second_key = second.vault_key
        first.clear_vault_key()
        self.assertEqual(second.vault_key, second_key)
        with self.assertRaises(first.VaultPasswordError):
            first.unlock_data_vault(NEW_PASSWORD)
        self.assertTrue(second.unlock_data_vault(NEW_PASSWORD))

    def test_shared_application_lock_and_warning_sink_are_explicit_dependencies(self):
        lock = threading.RLock()
        notices = []
        store = VaultStore(self.root / "shared", lock=lock, warning_sink=notices.append)
        self.assertIs(store.data_lock, lock)
        store._vault_recovery_warning("synthetic warning")
        store._vault_recovery_warning("synthetic warning")
        self.assertEqual(notices, ["synthetic warning"])
        self.assertEqual(store.load_warnings, ["synthetic warning"])
