"""Load the unmodified launcher into a disposable install/data directory."""

import importlib
import importlib.util
from pathlib import Path
import shutil
import tempfile
import sys
import uuid
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-only-original-password"
NEW_PASSWORD = "test-only-replacement-password"


class IsolatedLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = tempfile.TemporaryDirectory(prefix="xvviix-test-install-")
        source = Path(cls.install.name) / "game_launcher.py"
        shutil.copy2(PROJECT_ROOT / "game_launcher.py", source)
        package = Path(cls.install.name) / "xvviix"
        shutil.copytree(
            PROJECT_ROOT / "xvviix", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        cls.package_name = "_xvviix_test_" + cls.__name__ + "_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(
            cls.package_name,
            package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[cls.package_name] = module
        spec.loader.exec_module(module)
        cls.app = importlib.import_module(cls.package_name + ".app")

    @classmethod
    def tearDownClass(cls):
        for handler in list(cls.app.LOG.handlers):
            handler.close()
            cls.app.LOG.removeHandler(handler)
        for name in list(sys.modules):
            if name == cls.package_name or name.startswith(cls.package_name + "."):
                del sys.modules[name]
        cls.install.cleanup()

    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory(prefix="xvviix-test-data-")
        self.addCleanup(self.runtime.cleanup)
        app = self.app
        app.BASE_DIR = self.runtime.name
        app.SETTINGS_FILE = str(Path(self.runtime.name) / "launcher_settings.json")
        app.vault = app.make_vault_store(self.runtime.name)
        app.root = None
        # Test application boots must never register a real CI runner at login.
        app.startup_service = app.autostart.WindowsStartup(is_windows=False)
        app.settings_popup = None
        app.startup_menu_value = None
        app.startup_last_error = ""
        app.library_view = None
        app.activity_history = None
        app.activity_clock_job = None
        app._activity_rail_signature = None
        app.app_exit_event.clear()
        app.background_shutdown_started = False
        app.shutdown_dialog = None
        app.shutdown_started_at = None
        app.pending_launches.clear()
        app.shell_launches.clear()
        app.tracked_processes.clear()
        app.dirty_playtime_libraries.clear()
        app.dirty_icon_libraries.clear()
        app.dirty_scan_libraries.clear()
        app.icon_worker = None
        app.icon_requests.clear()
        app.icon_checked.clear()
        app.icon_save_job = None
        app.icon_save_running = False
        app.icon_dirty_versions = {"games": 0, "apps": 0, "founded": 0}
        app.vault.vault_key = None
        app.vault.vault_enabled = False
        app.data_loaded = False
        app.vault.load_warnings.clear()
        for name in ("games", "apps", "founded", "reports", "recent_activity"):
            setattr(app, name, [])
        self.addCleanup(app.vault.clear_vault_key)

    def make_vault(self, recoverable=True):
        app = self.app
        app.vault.create_data_vault(PASSWORD, allow_local_reset=recoverable)
        item = app.models.normalize_item(
            {
                "name": "Fictional regression-test game",
                "path": r"C:\Games\Example\game.exe",
                "playtime": 123,
                "color": "#123456",
            }
        )
        self.assertTrue(app.vault._save(app.vault.GAMES_FILE, [item]))
        return item

    def data_bytes(self):
        return {
            path: Path(path).read_bytes()
            for path in self.app.vault.protected_artifact_paths()
            if Path(path).is_file()
        }

    def settings_bytes(self):
        return {
            path: Path(path).read_bytes() if Path(path).is_file() else None
            for path in (
                self.app.vault.VAULT_FILE,
                self.app.vault.VAULT_FILE + ".bak",
                self.app.vault.LOCAL_RECOVERY_FILE,
            )
        }
