"""Source/frozen resource paths, independent of the process working directory."""

import os
import sys


def install_directory():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(relative_path):
    base = sys._MEIPASS if getattr(sys, "frozen", False) else install_directory()
    return os.path.join(base, relative_path)


def choose_data_dir(install_directory):
    """Use the project directory when writable, otherwise use per-user app data."""
    preferred = os.fspath(install_directory)
    try:
        os.makedirs(preferred, exist_ok=True)
        probe = os.path.join(preferred, ".xvviix-write-test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return preferred
    except OSError:
        fallback_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        fallback = os.path.join(fallback_root, "XVVIIXLauncher")
        os.makedirs(fallback, exist_ok=True)
        return fallback
