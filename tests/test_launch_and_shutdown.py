"""Slow process creation and live games must never hold the Tk event loop hostage."""

from pathlib import Path
import os
import ctypes
from ctypes import wintypes
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests.support import IsolatedLauncherTest, PASSWORD
from xvviix.services import launching


class LaunchBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix-launch-")
        self.addCleanup(self.directory.cleanup)
        self.program = Path(self.directory.name) / "game.exe"
        self.program.write_bytes(b"synthetic, never executed")

    def test_process_creation_does_not_wait_for_process_exit(self):
        process = Mock(pid=123)
        spawn = Mock(return_value=process)
        result = launching.launch_program(str(self.program), spawn=spawn)
        self.assertIs(result.process, process)
        process.wait.assert_not_called()
        process.poll.assert_not_called()
        spawn.assert_called_once_with(str(self.program))

    def test_validate_both_paths_before_starting_a_trainer(self):
        trainer = Path(self.directory.name) / "trainer.exe"
        trainer.write_bytes(b"synthetic")
        spawn = Mock()
        with self.assertRaises(launching.LaunchError):
            launching.launch_program(str(self.program) + "missing.exe", str(trainer), spawn=spawn)
        spawn.assert_not_called()

    def test_trainer_starts_before_program_without_waiting_for_its_lifetime(self):
        trainer = Path(self.directory.name) / "trainer.exe"
        trainer.write_bytes(b"synthetic")
        process = Mock(pid=123)
        spawn = Mock(return_value=process)
        launching.launch_program(str(self.program), str(trainer), spawn=spawn)
        self.assertEqual(
            [call.args[0] for call in spawn.call_args_list], [str(trainer), str(self.program)]
        )
        process.wait.assert_not_called()

    def test_elevation_is_requested_only_for_windows_elevation_required(self):
        error = OSError("synthetic elevation request")
        error.winerror = 740
        elevated = Mock(return_value=Mock(pid=123))
        result = launching.launch_program(
            str(self.program), spawn=Mock(side_effect=error), elevate=elevated
        )
        self.assertTrue(result.elevated)
        elevated.assert_called_once()

    def test_cancellation_before_launch_does_not_start_any_process(self):
        stop = threading.Event()
        stop.set()
        spawn = Mock()
        with self.assertRaises(launching.LaunchCancelled):
            launching.launch_program(str(self.program), cancel=stop, spawn=spawn)
        spawn.assert_not_called()

    def test_signalled_shell_process_can_exit_with_code_259(self):
        kernel = Mock()
        kernel.GetProcessId.return_value = 123
        kernel.WaitForSingleObject.return_value = 0

        def exit_code(_handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 259
            return True

        kernel.GetExitCodeProcess.side_effect = exit_code
        process = launching._ShellProcess(kernel, 456)
        self.assertEqual(process.wait(), 259)
        kernel.WaitForSingleObject.assert_called_once_with(456, 0)
        kernel.CloseHandle.assert_called_once_with(456)
        kernel.TerminateProcess.assert_not_called()

    def test_batch_target_is_not_interpolated_into_cmd_source(self):
        target = r"C:\Games\A & B\100% !game!.bat"
        popen = Mock()
        launching.spawn_command(target, popen=popen, windows=True)
        command = popen.call_args.args[0]
        self.assertNotIn(target, command)
        self.assertIn("/v:off", command)
        self.assertEqual(popen.call_args.kwargs["env"]["XVVIIX_BATCH_TARGET"], target)
        self.assertFalse(popen.call_args.kwargs["shell"])

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows cmd.exe")
    def test_native_batch_path_with_spaces_metacharacters_and_percent(self):
        path = Path(self.directory.name) / "A & B 100% !test!.bat"
        path.write_bytes(b'@echo off\r\necho success>"%~dp0marker.txt"\r\nexit /b 7\r\n')
        process = launching.spawn_command(str(path))
        self.assertEqual(process.wait(timeout=20), 7)
        self.assertEqual((Path(self.directory.name) / "marker.txt").read_text().strip(), "success")


class LaunchAndCloseUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app.root = self.root
        self.app.hardware_monitor = None
        self.app.background_music = None
        self.app.scan_running = False
        self.app.sys_report_running = False
        self.releases = []
        self.failures = []
        self.root.report_callback_exception = lambda _kind, value, _trace: self.failures.append(
            str(value)
        )
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        for event in self.releases:
            event.set()
        self.app.app_exit_event.set()
        for thread in threading.enumerate():
            if thread.name in {"program-launcher", "launcher-final-save", "test-slow-poll"}:
                thread.join(timeout=2)
        try:
            self.app.cancel_pending_callbacks(self.root)
            self.root.destroy()
        except self.app.tk.TclError:
            pass
        self.app.root = None
        self.assertEqual(self.failures, [])

    def pump(self, ms):
        self.root.after(ms, self.root.quit)
        self.root.mainloop()

    def test_slow_process_creation_keeps_ui_responsive_and_deduplicates_clicks(self):
        release, entered = threading.Event(), threading.Event()
        self.releases.append(release)
        threads, heartbeat = [], []

        def slow_launch(*_args, **_kwargs):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return self.app.launching.LaunchResult("synthetic.exe", Mock(pid=321), time.time())

        item = {"name": "Fixture", "path": "synthetic.exe"}
        with (
            patch.object(self.app.launching, "launch_program", side_effect=slow_launch) as launch,
            patch.object(self.app, "register_process") as register,
            patch.object(self.app, "refresh"),
        ):
            started = time.perf_counter()
            self.assertTrue(self.app.run_only(item))
            self.assertLess(time.perf_counter() - started, 0.3)
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.app.run_only(item))
            self.app.process_ui_queue()
            self.root.after(30, lambda: heartbeat.append(True))
            self.pump(120)
            self.assertEqual(heartbeat, [True])
            self.assertNotEqual(threads[0], threading.get_ident())
            release.set()
            self.pump(180)
            launch.assert_called_once()
            register.assert_called_once()

    def live_session(self):
        self.make_vault()
        self.app.load_all_data_files()
        item = self.app.games[0]
        process = Mock(pid=654)
        now = time.time()
        self.app.tracked_processes[654] = {
            "item": item,
            "path": item["path"],
            "created": now - 10,
            "started_at": now - 10,
            "last_accounted": now - 4,
            "ended_at": None,
            "process": process,
            "library_kind": "game",
            "exit_code": None,
        }
        return item, process

    def test_close_warns_saves_and_leaves_live_programs_running(self):
        item, process = self.live_session()
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=True) as confirm,
            patch.object(
                self.app,
                "_process_is_alive",
                side_effect=AssertionError("Shutdown must not query process liveness"),
            ),
        ):
            self.assertTrue(self.app.request_launcher_close())
            self.root.after(3000, self.root.quit)
            self.root.mainloop()
            confirm.assert_called_once()
        process.wait.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertIsNone(self.app.vault.vault_key)
        self.assertEqual(self.app.tracked_processes, {})
        self.app.vault.unlock_data_vault(PASSWORD)
        saved = self.app.vault.read_data_json(self.app.vault.GAMES_FILE)
        self.assertGreaterEqual(saved[0]["playtime"], 127)
        self.assertEqual(saved[0]["name"], item["name"])

    def test_a_real_running_child_is_not_a_dependency_of_launcher_shutdown(self):
        item, _fake = self.live_session()
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            session = self.app.tracked_processes.pop(654)
            session["process"] = process
            self.app.tracked_processes[process.pid] = session
            with patch.object(self.app.messagebox, "askyesno", return_value=True):
                self.app.request_launcher_close()
                self.root.after(3000, self.root.quit)
                self.root.mainloop()
            self.assertIsNone(process.poll(), "Closing XVVIIX must not close the user's process")
            self.assertIsNone(self.app.vault.vault_key)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    def test_cancel_close_keeps_tracking_active(self):
        self.live_session()
        with (
            patch.object(self.app.messagebox, "askyesno", return_value=False),
            patch.object(self.app, "_checkpoint_before_exit") as save,
        ):
            self.assertFalse(self.app.request_launcher_close())
            save.assert_not_called()
        self.assertFalse(self.app.app_exit_event.is_set())
        self.assertIn(654, self.app.tracked_processes)

    def test_slow_liveness_probe_holds_neither_model_nor_process_lock(self):
        self.live_session()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def slow_probe(*_args):
            for lock in (self.app.data_lock, self.app.process_lock):
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()
            entered.set()
            release.wait(3)
            return True

        with (
            patch.object(self.app, "_process_is_alive", side_effect=slow_probe),
            patch.object(self.app.messagebox, "askyesno", return_value=True),
        ):
            thread = threading.Thread(
                target=self.app.account_tracked_processes, daemon=True, name="test-slow-poll"
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            start = time.perf_counter()
            self.assertTrue(self.app.request_launcher_close())
            self.assertLess(time.perf_counter() - start, 0.5)
            self.root.after(2500, self.root.quit)
            self.root.mainloop()
            release.set()
            thread.join(timeout=2)
        self.assertIsNone(self.app.vault.vault_key)

    def test_slow_final_save_has_a_responsive_progress_dialog(self):
        release, entered = threading.Event(), threading.Event()
        self.releases.append(release)
        heartbeat = []

        def slow_save():
            entered.set()
            release.wait(3)
            return []

        with patch.object(self.app, "_checkpoint_before_exit", side_effect=slow_save):
            self.assertTrue(self.app.request_launcher_close())
            self.assertTrue(entered.wait(1))
            self.root.after(30, lambda: heartbeat.append(True))
            self.pump(100)
            self.assertEqual(heartbeat, [True])
            self.assertTrue(self.app.shutdown_dialog.busy)
            release.set()
            self.root.after(2000, self.root.quit)
            self.root.mainloop()
        self.assertTrue(self.app.shutdown_dialog.closed)
