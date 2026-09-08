"""Compatibility entry point for the XVVIIX desktop launcher.

Keep this file together with the xvviix/ package and the bundled assets.
Importing the entry point does not open windows or initialize user data.
"""

from pathlib import Path
import sys


_INCOMPLETE_INSTALL = (
    "XVVIIX installation is incomplete. Extract the entire project, "
    "including the xvviix folder, beside game_launcher.py."
)


def run():
    # Do not accidentally launch another copy from PYTHONPATH/site-packages.
    # Frozen distributions carry modules in their bundle, not as source files.
    if not getattr(sys, "frozen", False):
        if not (Path(__file__).resolve().parent / "xvviix" / "app.py").is_file():
            print(_INCOMPLETE_INSTALL, file=sys.stderr)
            return 1
    try:
        from xvviix.app import run as run_launcher
    except ModuleNotFoundError as exc:
        if exc.name == "xvviix" or (exc.name or "").startswith("xvviix."):
            print(_INCOMPLETE_INSTALL, file=sys.stderr)
            return 1
        raise
    return run_launcher()


if __name__ == "__main__":
    raise SystemExit(run())
