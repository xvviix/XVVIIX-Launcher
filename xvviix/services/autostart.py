"""Visible, per-user Windows login startup with a persistent opt-out.

No registry access occurs on import or construction. Only this application's
named values are changed; never HKLM, scheduled tasks, services or StartupApproved.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PREFERENCES_KEY = r"Software\XVVIIXLauncher\Preferences"
VALUE_NAME = "XVVIIXLauncher"
CHOICE_MARKER = "StartupChoiceMade"


class StartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupState:
    supported: bool
    registered: bool = False
    initialized: bool = False
    command: str = ""
    matches_current: bool = False


def build_command(install_directory, *, executable=None, frozen=None, exists=os.path.isfile):
    """Quote absolute executable/source paths; prefer this interpreter's pythonw."""
    executable = os.path.abspath(os.fspath(executable or sys.executable))
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if not exists(executable):
        raise StartupError("The current launcher executable could not be found")
    if frozen:
        arguments = [executable, "--autostart"]
    else:
        pythonw = str(Path(executable).with_name("pythonw.exe"))
        if Path(executable).name.casefold() in {"python.exe", "pythonw.exe"} and exists(pythonw):
            executable = pythonw
        entry = os.path.abspath(os.path.join(os.fspath(install_directory), "game_launcher.py"))
        package = os.path.join(os.fspath(install_directory), "xvviix", "app.py")
        if not exists(entry) or not exists(package):
            raise StartupError(
                "Keep the complete xvviix folder beside game_launcher.py before enabling startup"
            )
        arguments = [executable, entry, "--autostart"]
    command = subprocess.list2cmdline(arguments)
    if len(command.encode("utf-16-le")) // 2 > 260:
        raise StartupError(
            "The startup command is too long. Move the project to a shorter permanent folder and enable startup again."
        )
    return command


class WindowsStartup:
    """Manage one HKCU Run value and remember that the user has made a choice."""

    def __init__(
        self,
        registry=None,
        *,
        is_windows=None,
        run_key=RUN_KEY,
        preferences_key=PREFERENCES_KEY,
        value_name=VALUE_NAME,
    ):
        self.registry = winreg if registry is None else registry
        native = os.name == "nt" if is_windows is None else bool(is_windows)
        self.supported = bool(native and self.registry is not None)
        self.run_key = run_key
        self.preferences_key = preferences_key
        self.value_name = value_name
        self._lock = threading.RLock()

    def _require_windows(self):
        if not self.supported:
            raise StartupError("Start with Windows is available only on Windows")

    def _query(self, key_path, name):
        registry = self.registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_QUERY_VALUE
            ) as key:
                return registry.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def _write(self, key_path, name, value, kind):
        registry = self.registry
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_SET_VALUE
        ) as key:
            registry.SetValueEx(key, name, 0, kind, value)

    def _delete_run_value(self):
        registry = self.registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER, self.run_key, 0, registry.KEY_SET_VALUE
            ) as key:
                registry.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            pass

    def _state(self, expected_command=None):
        registration = self._query(self.run_key, self.value_name)
        marker = self._query(self.preferences_key, CHOICE_MARKER)
        command = str(registration[0]) if registration is not None else ""
        return StartupState(
            supported=True,
            registered=bool(command),
            initialized=marker is not None,
            command=command,
            matches_current=bool(command and expected_command and command == expected_command),
        )

    def state(self, expected_command=None):
        if not self.supported:
            return StartupState(supported=False)
        with self._lock:
            try:
                return self._state(expected_command)
            except OSError as exc:
                raise StartupError(f"Windows startup settings could not be read: {exc}") from exc

    def set_enabled(self, enabled, command=None):
        self._require_windows()
        registry = self.registry
        with self._lock:
            try:
                if not enabled:
                    # Mark the explicit choice before removal. A later start must
                    # never recreate the Run value simply because it is absent.
                    self._write(self.preferences_key, CHOICE_MARKER, 1, registry.REG_DWORD)
                    self._delete_run_value()
                    state = self._state(command)
                    if state.registered:
                        raise StartupError("The Windows startup entry is still present")
                    return state
                if not isinstance(command, str) or not command.strip() or "\x00" in command:
                    raise StartupError("A valid startup command is required")
                previous = self._query(self.run_key, self.value_name)
                self._write(self.run_key, self.value_name, command, registry.REG_SZ)
                try:
                    self._write(self.preferences_key, CHOICE_MARKER, 1, registry.REG_DWORD)
                except OSError:
                    # Do not report success if the durable choice could not be saved.
                    try:
                        if previous is None:
                            self._delete_run_value()
                        else:
                            self._write(self.run_key, self.value_name, previous[0], previous[1])
                    except OSError as rollback:
                        raise StartupError(
                            "Startup settings could not be saved or restored; inspect the XVVIIX entry in Windows Startup Apps."
                        ) from rollback
                    raise
                state = self._state(command)
                if not state.matches_current:
                    raise StartupError("Windows did not retain the requested startup command")
                return state
            except OSError as exc:
                raise StartupError(f"Windows startup could not be changed: {exc}") from exc

    def initialize_once(self, command):
        """Default on once, but honor prior app opt-out or external entry removal."""
        self._require_windows()
        with self._lock:
            try:
                state = self._state(command)
                if state.initialized:
                    return state, False
                # A pre-existing entry is adopted, not silently overwritten.
                if state.registered:
                    self._write(self.preferences_key, CHOICE_MARKER, 1, self.registry.REG_DWORD)
                    return self._state(command), False
                return self.set_enabled(True, command), True
            except OSError as exc:
                raise StartupError(f"Windows startup could not be initialized: {exc}") from exc
