"""Only owned HKCU values may change; an explicit startup opt-out must stick."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from xvviix.services import autostart
from tests.support import IsolatedLauncherTest


class FakeKey:
    def __init__(self, root, path):
        self.root, self.path = root, path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE, KEY_SET_VALUE = 1, 2
    REG_SZ, REG_DWORD = 1, 4

    def __init__(self):
        self.values = {}
        self.operations = []
        self.fail_write = None
        self.fail_delete = False
        self.fail_read = False

    def OpenKey(self, root, path, _reserved, access):
        self.operations.append(("open", root, path, access))
        if self.fail_read:
            raise PermissionError("Synthetic registry read denial")
        if (root, path) not in self.values:
            raise FileNotFoundError(path)
        return FakeKey(root, path)

    def CreateKeyEx(self, root, path, _reserved, access):
        self.operations.append(("create", root, path, access))
        self.values.setdefault((root, path), {})
        return FakeKey(root, path)

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key.root, key.path)][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _reserved, kind, value):
        self.operations.append(("set", key.root, key.path, name, value, kind))
        if self.fail_write == (key.path, name):
            raise PermissionError("Synthetic registry write denial")
        self.values[(key.root, key.path)][name] = (value, kind)

    def DeleteValue(self, key, name):
        self.operations.append(("delete", key.root, key.path, name))
        if self.fail_delete:
            raise PermissionError("Synthetic registry delete denial")
        try:
            del self.values[(key.root, key.path)][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def run_values(self):
        return self.values.setdefault((self.HKEY_CURRENT_USER, autostart.RUN_KEY), {})


COMMAND = '"C:\\Apps\\XVVIIX\\launcher.exe" --autostart'


class StartupRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.service = autostart.WindowsStartup(self.registry, is_windows=True)

    def test_constructor_does_not_touch_the_registry(self):
        self.assertEqual(self.registry.operations, [])

    def test_default_registration_happens_once_and_only_for_current_user(self):
        state, added = self.service.initialize_once(COMMAND)
        self.assertTrue(added)
        self.assertTrue(state.registered)
        self.assertTrue(state.matches_current)
        self.assertEqual(
            self.registry.run_values()[autostart.VALUE_NAME], (COMMAND, self.registry.REG_SZ)
        )
        writes = sum(operation[0] == "set" for operation in self.registry.operations)
        state, added = self.service.initialize_once(COMMAND)
        self.assertFalse(added)
        self.assertTrue(state.initialized)
        self.assertEqual(
            sum(operation[0] == "set" for operation in self.registry.operations), writes
        )
        self.assertTrue(all(operation[1] == "HKCU" for operation in self.registry.operations))

    def test_disable_keeps_other_startup_entries_and_is_not_auto_reenabled(self):
        self.registry.run_values()["AnotherApp"] = ("unrelated.exe", self.registry.REG_SZ)
        self.service.initialize_once(COMMAND)
        self.assertFalse(self.service.set_enabled(False).registered)
        self.assertEqual(
            self.registry.run_values(), {"AnotherApp": ("unrelated.exe", self.registry.REG_SZ)}
        )
        new_instance = autostart.WindowsStartup(self.registry, is_windows=True)
        state, added = new_instance.initialize_once(COMMAND)
        self.assertFalse(state.registered)
        self.assertFalse(added)
        self.assertTrue(state.initialized)

    def test_disable_before_first_auto_initialization_is_also_sticky(self):
        self.service.set_enabled(False)
        state, added = self.service.initialize_once(COMMAND)
        self.assertFalse(state.registered)
        self.assertFalse(added)

    def test_external_removal_is_not_silently_undone(self):
        self.service.initialize_once(COMMAND)
        self.registry.run_values().pop(autostart.VALUE_NAME)
        state, added = self.service.initialize_once(COMMAND)
        self.assertFalse(state.registered)
        self.assertFalse(added)

    def test_preexisting_command_is_adopted_not_overwritten(self):
        old = '"D:\\Other copy\\launcher.exe" --autostart'
        self.registry.run_values()[autostart.VALUE_NAME] = (old, self.registry.REG_SZ)
        state, added = self.service.initialize_once(COMMAND)
        self.assertFalse(added)
        self.assertFalse(state.matches_current)
        self.assertEqual(state.command, old)
        self.assertTrue(self.service.set_enabled(True, COMMAND).matches_current)

    def test_failed_run_write_does_not_report_success(self):
        self.registry.fail_write = (autostart.RUN_KEY, autostart.VALUE_NAME)
        with self.assertRaises(autostart.StartupError):
            self.service.set_enabled(True, COMMAND)
        self.assertNotIn(autostart.VALUE_NAME, self.registry.run_values())

    def test_failed_choice_marker_rolls_back_the_startup_command(self):
        old = "old-owned-command.exe"
        self.registry.run_values()[autostart.VALUE_NAME] = (old, self.registry.REG_SZ)
        self.registry.fail_write = (autostart.PREFERENCES_KEY, autostart.CHOICE_MARKER)
        with self.assertRaises(autostart.StartupError):
            self.service.set_enabled(True, COMMAND)
        self.assertEqual(self.registry.run_values()[autostart.VALUE_NAME][0], old)

    def test_failed_delete_is_not_shown_as_disabled(self):
        self.service.initialize_once(COMMAND)
        self.registry.fail_delete = True
        with self.assertRaises(autostart.StartupError):
            self.service.set_enabled(False)
        self.assertTrue(self.service.state().registered)

    def test_read_denial_is_not_treated_as_an_empty_registry(self):
        self.registry.fail_read = True
        with self.assertRaises(autostart.StartupError):
            self.service.initialize_once(COMMAND)
        self.assertFalse(any(operation[0] == "set" for operation in self.registry.operations))

    def test_non_windows_does_nothing(self):
        service = autostart.WindowsStartup(self.registry, is_windows=False)
        self.assertFalse(service.state().supported)
        with self.assertRaises(autostart.StartupError):
            service.set_enabled(True, COMMAND)
        self.assertEqual(self.registry.operations, [])


class StartupCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix startup test ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.python = self.root / ".venv/Scripts/python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"test executable placeholder")
        (self.root / "game_launcher.py").write_text("# test placeholder\n")
        (self.root / "xvviix").mkdir()
        (self.root / "xvviix/app.py").write_text("# test placeholder\n")

    def test_source_command_uses_matching_pythonw_and_quoted_absolute_paths(self):
        pythonw = self.python.with_name("pythonw.exe")
        pythonw.write_bytes(b"test placeholder")
        command = autostart.build_command(self.root, executable=self.python, frozen=False)
        expected = subprocess.list2cmdline(
            [str(pythonw), str(self.root / "game_launcher.py"), "--autostart"]
        )
        self.assertEqual(command, expected)
        self.assertNotIn("START_XVVIIX.bat", command)
        self.assertNotIn("password", command.casefold())

    def test_missing_pythonw_uses_the_same_python_not_a_global_fallback(self):
        command = autostart.build_command(self.root, executable=self.python, frozen=False)
        self.assertIn(str(self.python), command)

    def test_frozen_command_uses_the_real_launcher_binary_only(self):
        executable = self.root / "XVVIIX Launcher.exe"
        executable.write_bytes(b"test placeholder")
        command = autostart.build_command(self.root, executable=executable, frozen=True)
        self.assertEqual(command, subprocess.list2cmdline([str(executable), "--autostart"]))
        self.assertNotIn("game_launcher.py", command)

    def test_incomplete_source_install_is_not_registered(self):
        (self.root / "xvviix/app.py").unlink()
        with self.assertRaises(autostart.StartupError):
            autostart.build_command(self.root, executable=self.python, frozen=False)

    def test_overlong_command_is_reported_instead_of_silently_truncated(self):
        with self.assertRaisesRegex(autostart.StartupError, "too long"):
            autostart.build_command(
                "C:/" + "long/" * 70,
                executable="C:/python/python.exe",
                frozen=False,
                exists=lambda _path: True,
            )


class StartupUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.app.root = self.root
        self.registry = FakeRegistry()
        self.app.startup_service = self.app.autostart.WindowsStartup(self.registry, is_windows=True)
        self.command_patch = patch.object(
            self.app, "_current_startup_command", return_value=COMMAND
        )
        self.command_patch.start()
        self.activity_patch = patch.object(self.app, "add_activity")
        self.activity = self.activity_patch.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.app.root = None
        self.command_patch.stop()
        self.activity_patch.stop()

    def test_initialization_waits_for_successful_unlock_and_is_not_repeated(self):
        self.app.data_loaded = False
        self.app.initialize_windows_startup()
        self.assertFalse(self.registry.operations)
        self.app.data_loaded = True
        self.app.initialize_windows_startup()
        self.app.initialize_windows_startup()
        self.assertTrue(self.app.startup_service.state().registered)
        self.activity.assert_called_once()

    def test_settings_checkbox_enables_and_disables_without_reenabling_later(self):
        self.app.data_loaded = True
        menu = self.app._prepare_settings_menu()
        self.assertEqual(menu.entrycget(0, "label"), "Start with Windows")
        menu.invoke(0)
        self.assertTrue(self.app.startup_service.state().registered)
        self.assertTrue(self.app.startup_menu_value.get())
        self.assertIs(self.app._prepare_settings_menu(), menu)
        menu.invoke(0)
        self.assertFalse(self.app.startup_service.state().registered)
        self.app.initialize_windows_startup()
        self.assertFalse(self.app.startup_service.state().registered)
        self.assertFalse(self.app.startup_menu_value.get())

    def test_checkbox_reverts_when_registry_change_fails(self):
        self.app.startup_service.set_enabled(True, COMMAND)
        menu = self.app._prepare_settings_menu()
        self.registry.fail_delete = True
        with patch.object(self.app.messagebox, "showerror") as error:
            menu.invoke(0)
        error.assert_called_once()
        self.assertTrue(self.app.startup_menu_value.get())

    def test_invalid_current_path_does_not_prevent_disabling_an_existing_entry(self):
        self.app.startup_service.set_enabled(True, COMMAND)
        with patch.object(
            self.app,
            "_current_startup_command",
            side_effect=self.app.autostart.StartupError("path is too long"),
        ):
            menu = self.app._prepare_settings_menu()
            self.assertEqual(menu.entrycget(0, "state"), "normal")
            menu.invoke(0)
        self.assertFalse(self.app.startup_service.state().registered)

    def test_linux_menu_is_explicitly_disabled(self):
        self.app.startup_service = self.app.autostart.WindowsStartup(
            self.registry, is_windows=False
        )
        menu = self.app._prepare_settings_menu()
        self.assertEqual(menu.entrycget(0, "state"), "disabled")
        menu.invoke(0)
        self.assertFalse(self.registry.operations)

    def test_other_copy_can_be_updated_only_by_an_explicit_action(self):
        self.app.startup_service.set_enabled(True, "old-owned-command.exe")
        menu = self.app._prepare_settings_menu()
        labels = [
            menu.entrycget(index, "label")
            for index in range(menu.index("end") + 1)
            if menu.type(index) != "separator"
        ]
        self.assertIn("Use this copy at Windows startup", labels)
        self.assertEqual(self.app.startup_service.state().command, "old-owned-command.exe")


@unittest.skipUnless(os.name == "nt", "Native Windows registry API")
class NativeStartupRegistryTests(unittest.TestCase):
    def test_native_roundtrip_uses_a_private_test_key_not_real_login_startup(self):
        import winreg

        prefix = "Software\\XVVIIXLauncherTests\\" + uuid.uuid4().hex
        run_key, preferences = prefix + r"\Run", prefix + r"\Preferences"
        service = autostart.WindowsStartup(
            run_key=run_key, preferences_key=preferences, value_name="test-only"
        )
        try:
            state, added = service.initialize_once(COMMAND)
            self.assertTrue(added)
            self.assertTrue(state.registered)
            self.assertFalse(service.set_enabled(False).registered)
            self.assertFalse(service.initialize_once(COMMAND)[1])
        finally:
            for path in (run_key, preferences, prefix):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                except FileNotFoundError:
                    pass
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\XVVIIXLauncherTests")
            except OSError:
                pass
