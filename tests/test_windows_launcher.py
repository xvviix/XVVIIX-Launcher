"""Exercise the real batch file on Windows, using a harmless temporary launcher."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import PROJECT_ROOT


@unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix batch !bang! ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copy2(PROJECT_ROOT / "START_XVVIIX.bat", self.root / "START_XVVIIX.bat")
        self.cmd = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")

    def dummy_source(self, exit_code=0):
        (self.root / "game_launcher.py").write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path('run.json').write_text(json.dumps({'prefix': sys.prefix, 'executable': sys.executable}), encoding='utf-8')\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )

    def make_venv(self):
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")],
            check=True,
            capture_output=True,
            timeout=60,
        )

    def run_batch(self, env=None):
        return subprocess.run(
            [self.cmd, "/d", "/c", "START_XVVIIX.bat"],
            cwd=self.root,
            env=env,
            input="\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )

    def test_project_venv_takes_priority_even_in_path_with_spaces_and_exclamation(self):
        self.dummy_source()
        self.make_venv()
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(record["prefix"]).resolve(), (self.root / ".venv").resolve())
        self.assertIn("project's Windows virtual environment", result.stdout)

    def test_python_exit_code_is_preserved_after_error_diagnostics(self):
        self.dummy_source(exit_code=23)
        self.make_venv()
        result = self.run_batch()
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn("exited with code 23", result.stdout)

    def test_missing_source_is_reported_before_trying_to_launch(self):
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("game_launcher.py was not found", result.stdout)
        self.assertFalse((self.root / "run.json").exists())

    def test_missing_python_has_clear_install_instructions(self):
        self.dummy_source()
        env = dict(os.environ)
        env["PATH"] = str(Path(env["SystemRoot"]) / "System32")
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        result = self.run_batch(env=env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Python was not found", result.stdout)
        self.assertIn("tcl/tk and IDLE", result.stdout)
        self.assertFalse((self.root / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
