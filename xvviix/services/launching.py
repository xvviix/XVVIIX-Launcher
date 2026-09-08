"""Process creation helpers. Call from a worker, never from the Tk event thread."""

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import ntpath
import os
import subprocess
import threading
import time

from ..utils import clean_path


class LaunchError(Exception):
    pass


class LaunchCancelled(LaunchError):
    pass


@dataclass
class LaunchResult:
    path: str
    process: object | None
    started_at: float
    elevated: bool = False


def validate_executable(path):
    path = clean_path(path)
    if ntpath.splitext(path)[1].lower() not in {".exe", ".bat"}:
        raise LaunchError("Only .exe and .bat files are supported")
    if not os.path.isfile(path):
        raise LaunchError(f"File not found:\n{path}")
    return path


def spawn_command(path, *, popen=None, windows=None):
    """Use an argv list for EXEs; isolate Windows batch paths from cmd syntax."""
    windows = os.name == "nt" if windows is None else windows
    popen = subprocess.Popen if popen is None else popen
    cwd = os.path.dirname(path) or None
    if ntpath.splitext(path)[1].lower() == ".bat":
        if not windows:
            raise LaunchError("Batch launchers require Windows")
        processor = os.environ.get("COMSPEC") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
        )
        if '"' in path or "\x00" in path:
            raise LaunchError("Invalid batch path")
        environment = dict(os.environ)
        environment["XVVIIX_BATCH_TARGET"] = path
        # No CALL / delayed expansion: literal %, ! and & in filenames must not
        # become another command or a second round of environment expansion.
        command = f'"{processor}" /d /v:off /s /c ""%XVVIIX_BATCH_TARGET%""'
        return popen(command, executable=processor, cwd=cwd, env=environment, shell=False)
    return popen([path], cwd=cwd, shell=False)


class _ShellProcess:
    """A waitable ShellExecuteEx handle. Detaching never terminates the child."""

    def __init__(self, kernel, handle):
        self._kernel = kernel
        self._handle = handle
        self._lock = threading.Lock()
        self._detached = threading.Event()
        self.pid = int(kernel.GetProcessId(handle))
        self.returncode = None

    def poll(self):
        with self._lock:
            if not self._handle:
                return self.returncode
            state = self._kernel.WaitForSingleObject(self._handle, 0)
            if state == 258:  # WAIT_TIMEOUT: the process is still running
                return None
            if state != 0:
                raise OSError(ctypes.get_last_error(), "Could not query launched process")
            code = wintypes.DWORD()
            if not self._kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                raise OSError(ctypes.get_last_error(), "Could not query launched process")
            # 259 is a valid exit code too; the handle's signalled state decides.
            self.returncode = int(code.value)
            return self.returncode

    def wait(self):
        while not self._detached.is_set():
            code = self.poll()
            if code is not None:
                self.detach()
                return code
            self._detached.wait(0.15)
        raise OSError("Launcher detached from the process")

    def detach(self):
        self._detached.set()
        with self._lock:
            if self._handle:
                self._kernel.CloseHandle(self._handle)
                self._handle = None

    def terminate(self):
        with self._lock:
            if self._handle and not self._kernel.TerminateProcess(self._handle, 1):
                raise OSError(ctypes.get_last_error(), "Could not terminate process")

    kill = terminate


def spawn_elevated(path, cwd=None):
    """Let Windows show its normal UAC prompt; no credential collection or bypass."""
    if os.name != "nt":
        raise LaunchError("Administrator launching is available only on Windows")

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = (
        0x40 | 0x100 | 0x400
    )  # keep process handle, synchronous shell dispatch, no duplicate error UI
    info.lpVerb = "runas"
    info.lpFile = path
    info.lpDirectory = cwd
    info.nShow = 1
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    initialized = ole.CoInitializeEx(None, 2) in (0, 1)
    try:
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            code = ctypes.get_last_error()
            if code == 1223:
                raise LaunchCancelled("Administrator permission was cancelled")
            raise LaunchError(f"Windows could not start the program (error {code})")
        process = _ShellProcess(kernel, info.hProcess) if info.hProcess else None
        if process is not None and not process.pid:
            process.detach()
            return None
        return process
    finally:
        if initialized:
            ole.CoUninitialize()


def launch_program(path, trainer="", *, cancel=None, spawn=None, elevate=None):
    """Validate and create processes without waiting for their windows or lifetime."""
    spawn = spawn_command if spawn is None else spawn
    elevate = spawn_elevated if elevate is None else elevate
    cancelled = cancel.is_set if cancel is not None else lambda: False
    if cancelled():
        raise LaunchCancelled("Launcher is closing")
    path = validate_executable(path)
    trainer = validate_executable(trainer) if trainer else ""
    if cancelled():
        raise LaunchCancelled("Launcher is closing")
    if trainer:
        try:
            spawn(trainer)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 740:
                elevate(trainer, os.path.dirname(trainer) or None)
            else:
                raise LaunchError(f"Could not start trainer:\n{exc}") from exc
    if cancelled():
        raise LaunchCancelled("Launcher is closing")
    started = time.time()
    try:
        process = spawn(path)
        return LaunchResult(path, process, started)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            process = elevate(path, os.path.dirname(path) or None)
            return LaunchResult(path, process, started, elevated=True)
        raise LaunchError(f"Could not start program:\n{exc}") from exc
