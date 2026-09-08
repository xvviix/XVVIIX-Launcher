"""Full launcher reset/startup/shutdown regression with isolated libraries."""

from pathlib import Path
import shutil
import threading
from unittest.mock import patch

from tests.support import IsolatedLauncherTest, PROJECT_ROOT, NEW_PASSWORD, PASSWORD


class LauncherStartupTests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            probe = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        probe.destroy()
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        self.app.monitor_stop.set()
        for window in self.windows:
            try:
                for job in window.tk.splitlist(window.tk.call("after", "info")):
                    window.after_cancel(job)
            except self.app.tk.TclError:
                pass
            try:
                window.destroy()
            except self.app.tk.TclError:
                pass
        for thread in threading.enumerate():
            if thread.name in {"playtime-monitor", "vault-password-reset"}:
                thread.join(timeout=2)

    def test_reset_with_damaged_backup_reaches_main_window_and_exits_cleanly(self):
        app = self.app
        expected = self.make_vault()
        expected["artwork"] = str(Path(self.install.name) / "assets" / "xvviix_header.png")
        self.assertTrue(app.vault._save(app.vault.GAMES_FILE, [expected]))
        app.vault._atomic_write_bytes(
            app.vault.GAMES_FILE + ".bak", b"damaged integration-test backup"
        )
        data = self.data_bytes()
        app.vault.clear_vault_key()
        shutil.copytree(
            PROJECT_ROOT / "assets", Path(self.install.name) / "assets", dirs_exist_ok=True
        )
        shutil.copy2(PROJECT_ROOT / "icon.ico", Path(self.install.name) / "icon.ico")
        app.vault._atomic_write_bytes(app.SETTINGS_FILE, b'{"music_enabled": false}')
        original_tk = app.tk.Tk
        failures, notices = [], []

        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)

        def find(window, name):
            return next(widget for widget in walk(window) if widget.winfo_name() == name)

        def make_window():
            window = original_tk()
            self.windows.append(window)

            def fail(error):
                failures.append(str(error))
                window.destroy()

            window.report_callback_exception = lambda _kind, value, _trace: fail(value)

            def guarded(callback):
                def run():
                    try:
                        callback()
                    except Exception as exc:
                        fail(exc)

                return run

            def reset_driver():
                find(window, "new_password").insert(0, NEW_PASSWORD)
                find(window, "confirm_password").insert(0, NEW_PASSWORD)
                find(window, "submit_reset").invoke()

            def login_driver():
                window.after(100, guarded(reset_driver))
                find(window, "forgot_password").invoke()

            def close_when_ready():
                ready = any(
                    record["phase"] == "MAIN_EVENT_LOOP" and record["status"] == "READY"
                    for record in app.startup_diagnostics
                )
                if not ready or not notices or app.card_art_waiters:
                    window.after(50, guarded(close_when_ready))
                    return
                self.assertEqual(app.games, [expected])
                self.assertTrue(any(image is not None for image in app.card_art_cache.values()))
                window.after_cancel(watchdog_job)
                close_command = window.protocol("WM_DELETE_WINDOW")
                self.assertTrue(close_command)
                window.tk.call(close_command)

            window.after(100, guarded(login_driver))
            window.after(500, guarded(close_when_ready))
            watchdog_job = window.after(
                9000, lambda: fail("Launcher did not become ready within 9 seconds")
            )
            return window

        # Test the real application shell but not DND/audio device integrations.
        # Unexpected native dialogs must fail the test rather than hang a CI runner.
        with (
            patch.object(app, "HAS_DND", False),
            patch.object(app.tk, "Tk", side_effect=make_window),
            patch.object(
                app.messagebox,
                "showwarning",
                side_effect=lambda title, message, **kwargs: notices.append(message),
            ),
            patch.object(
                app.messagebox,
                "showerror",
                side_effect=lambda *args, **kwargs: failures.append(str(args)),
            ),
            patch.object(
                app.messagebox,
                "showinfo",
                side_effect=lambda *args, **kwargs: failures.append(str(args)),
            ),
            patch.object(
                app, "report_startup_error", side_effect=lambda message: failures.append(message)
            ),
        ):
            exit_code = app.run()
        self.assertEqual(exit_code, 0, failures)
        self.assertEqual(failures, [])
        self.assertTrue(any("games.json.bak" in notice for notice in notices))
        self.assertEqual(self.data_bytes(), data)
        self.assertIsNone(app.vault.vault_key)
        self.assertIsNone(app.card_art_worker)
        metadata = app.vault.load_vault_metadata()
        self.assertTrue(app.vault.verify_vault_password(NEW_PASSWORD, metadata))
        with self.assertRaises(app.vault.VaultPasswordError):
            app.vault.verify_vault_password(PASSWORD, metadata)
