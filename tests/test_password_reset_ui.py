"""Tk modal regression tests; skipped when a graphical display is unavailable."""

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests.support import IsolatedLauncherTest, NEW_PASSWORD, PASSWORD


class PasswordResetUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.title("XVVIIX isolated UI tests")
        self.root.geometry("1x1+0+0")
        self.app.root = self.root
        self.callback_errors = []
        self.root.report_callback_exception = lambda _kind, value, _traceback: self.fail_callback(
            value
        )

    def tearDown(self):
        for thread in threading.enumerate():
            if thread.name == "vault-password-reset":
                thread.join(timeout=6)
        if hasattr(self, "root"):
            self.app.cancel_all_animations()
            self.root.destroy()
            self.app.root = None

    def widgets(self, parent=None):
        parent = self.root if parent is None else parent
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def widget(self, name):
        matches = [widget for widget in self.widgets() if widget.winfo_name() == name]
        self.assertEqual(len(matches), 1, f"Expected exactly one widget named {name}")
        return matches[0]

    def fail_callback(self, error):
        self.callback_errors.append(error)
        for child in list(self.root.winfo_children()):
            if isinstance(child, self.app.tk.Toplevel):
                child.destroy()

    def guarded(self, callback):
        def run():
            try:
                callback()
            except Exception as exc:
                self.fail_callback(exc)

        return run

    def run_modal(self, function, driver):
        driver_id = self.root.after(100, self.guarded(driver))
        timeout_id = self.root.after(
            10000,
            lambda: self.fail_callback(AssertionError("Modal did not finish within 10 seconds")),
        )
        try:
            result = function()
        finally:
            for job in (driver_id, timeout_id):
                self.root.after_cancel(job)
        if self.callback_errors:
            raise self.callback_errors[0]
        return result

    def fill_reset(self, password=NEW_PASSWORD, confirmation=None):
        for name, value in (
            ("new_password", password),
            ("confirm_password", password if confirmation is None else confirmation),
        ):
            entry = self.widget(name)
            entry.delete(0, "end")
            entry.insert(0, value)

    def assert_controls_fit(self, dialog):
        dialog.update_idletasks()
        x, y = dialog.winfo_rootx(), dialog.winfo_rooty()
        width, height = dialog.winfo_width(), dialog.winfo_height()
        self.assertLessEqual(height, dialog.winfo_screenheight())
        for widget in self.widgets(dialog):
            if not isinstance(
                widget, (self.app.tk.Entry, self.app.tk.Button, self.app.tk.Checkbutton)
            ):
                continue
            self.assertTrue(widget.winfo_ismapped(), widget.winfo_name())
            self.assertGreaterEqual(widget.winfo_rootx(), x)
            self.assertGreaterEqual(widget.winfo_rooty(), y)
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), x + width)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), y + height)

    def test_setup_defaults_to_visible_local_reset_option_and_creates_recoverable_vault(self):
        def driver():
            self.assert_controls_fit(self.widget("vault_dialog"))
            option = self.widget("local_reset_opt_in")
            self.assertTrue(self.root.getboolean(option.getvar(option.cget("variable"))))
            self.widget("master_password").insert(0, PASSWORD)
            self.widget("confirm_password").insert(0, PASSWORD)
            self.widget("submit_vault").invoke()

        self.assertTrue(
            self.run_modal(lambda: self.app.show_vault_dialog(self.root, setup=True), driver)
        )
        self.assertTrue(self.app.vault.local_password_reset_status()[0])
        self.assertTrue(self.app.data_loaded)

    def test_extracted_dialog_uses_the_current_application_palette(self):
        with patch.object(self.app, "BG", "#102030"):

            def driver():
                self.assertEqual(self.widget("vault_dialog").cget("bg"), "#102030")
                self.widget("exit_vault").invoke()

            self.assertFalse(
                self.run_modal(lambda: self.app.show_vault_dialog(self.root, setup=True), driver)
            )

    def test_root_cleanup_cancels_pending_callbacks_without_deleting_widget_commands(self):
        callbacks = []
        job = self.root.after(1000, lambda: callbacks.append("unexpected"))
        self.assertIn(job, self.root.tk.splitlist(self.root.tk.call("after", "info")))
        self.assertGreaterEqual(self.app.cancel_pending_callbacks(self.root), 1)
        self.assertNotIn(job, self.root.tk.splitlist(self.root.tk.call("after", "info")))
        self.root.update()
        self.assertEqual(callbacks, [])

    def test_widget_owned_values_release_tcl_references_on_destroy(self):
        dialog = self.app.tk.Toplevel(self.root)
        value = self.app.DialogVariable(dialog, "fixture")
        changed = []
        value.trace_add("write", lambda *_args: changed.append(True))
        value.set("updated")
        self.assertEqual(value.get(), "updated")
        self.assertEqual(changed, [True])
        name = str(value)
        dialog.destroy()
        self.assertIsNone(value._tk)
        self.assertIsNone(value._master)
        self.assertFalse(self.root.tk.getboolean(self.root.tk.call("info", "exists", name)))

    def test_setup_can_keep_password_only_protection(self):
        def driver():
            self.widget("local_reset_opt_in").invoke()
            self.widget("master_password").insert(0, PASSWORD)
            self.widget("confirm_password").insert(0, PASSWORD)
            self.widget("submit_vault").invoke()

        self.assertTrue(
            self.run_modal(lambda: self.app.show_vault_dialog(self.root, setup=True), driver)
        )
        self.assertFalse(self.app.vault.local_password_reset_status()[0])
        self.assertEqual(self.app.vault.load_vault_metadata()["format"], "xvviix-vault-v1")

    def test_existing_vault_migration_requires_explicit_checkbox_and_correct_password(self):
        self.make_vault(recoverable=False)
        data = self.data_bytes()
        self.app.vault.clear_vault_key()

        def driver():
            self.assert_controls_fit(self.widget("vault_dialog"))
            option = self.widget("local_reset_opt_in")
            self.assertFalse(self.root.getboolean(option.getvar(option.cget("variable"))))
            option.invoke()
            self.widget("master_password").insert(0, PASSWORD)
            self.widget("submit_vault").invoke()

        self.assertTrue(self.run_modal(lambda: self.app.show_vault_dialog(self.root), driver))
        self.assertTrue(self.app.vault.local_password_reset_status()[0])
        self.assertEqual(self.data_bytes(), data)

    def test_regular_legacy_sign_in_keeps_recovery_disabled(self):
        self.make_vault(recoverable=False)
        self.app.vault.clear_vault_key()

        def driver():
            self.widget("master_password").insert(0, PASSWORD)
            self.widget("submit_vault").invoke()

        self.assertTrue(self.run_modal(lambda: self.app.show_vault_dialog(self.root), driver))
        self.assertFalse(self.app.vault.local_password_reset_status()[0])

    def test_full_forgot_password_flow_unlocks_existing_libraries(self):
        item = self.make_vault()
        data = self.data_bytes()
        self.app.vault.clear_vault_key()

        def reset_driver():
            self.assert_controls_fit(self.widget("password_reset"))
            self.fill_reset()
            self.widget("submit_reset").invoke()

        def login_driver():
            self.assert_controls_fit(self.widget("vault_dialog"))
            self.root.after(100, self.guarded(reset_driver))
            self.widget("forgot_password").invoke()

        self.assertTrue(self.run_modal(lambda: self.app.show_vault_dialog(self.root), login_driver))
        self.assertTrue(self.app.data_loaded)
        self.assertEqual(self.app.games, [item])
        self.assertEqual(self.data_bytes(), data)
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.unlock_data_vault(NEW_PASSWORD))

    def test_forgot_password_ignores_broken_backup_but_keeps_its_bytes(self):
        item = self.make_vault()
        backup = self.app.vault.GAMES_FILE + ".bak"
        self.app.vault._atomic_write_bytes(backup, b"damaged backup fixture")
        data = self.data_bytes()
        self.app.vault.clear_vault_key()

        def reset_driver():
            self.fill_reset()
            self.widget("submit_reset").invoke()

        def login_driver():
            self.root.after(100, self.guarded(reset_driver))
            self.widget("forgot_password").invoke()

        self.assertTrue(self.run_modal(lambda: self.app.show_vault_dialog(self.root), login_driver))
        self.assertEqual(self.app.games, [item])
        self.assertEqual(self.data_bytes(), data)
        self.assertEqual(Path(backup).read_bytes(), b"damaged backup fixture")
        self.assertTrue(
            any("games.json.bak" in message for message in self.app.vault.load_warnings)
        )

    def test_reset_rejects_short_or_mismatched_password_then_succeeds(self):
        self.make_vault()
        self.app.vault.clear_vault_key()

        def driver():
            self.fill_reset("short")
            self.widget("submit_reset").invoke()
            self.assertIn("8 TO 1024", self.widget("reset_status").cget("text"))
            self.fill_reset(confirmation="a-different-confirmation")
            self.widget("submit_reset").invoke()
            self.assertIn("DO NOT MATCH", self.widget("reset_status").cget("text"))
            self.fill_reset()
            self.widget("submit_reset").invoke()

        self.assertTrue(
            self.run_modal(lambda: self.app.show_password_reset_dialog(self.root), driver)
        )
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.unlock_data_vault(NEW_PASSWORD))

    def test_cancel_changes_neither_password_nor_data(self):
        self.make_vault()
        before, data = self.settings_bytes(), self.data_bytes()
        self.app.vault.clear_vault_key()

        def driver():
            self.fill_reset()
            self.widget("cancel_reset").invoke()

        self.assertFalse(
            self.run_modal(lambda: self.app.show_password_reset_dialog(self.root), driver)
        )
        self.assertEqual(self.settings_bytes(), before)
        self.assertEqual(self.data_bytes(), data)
        self.assertIsNone(self.app.vault.vault_key)

    def test_legacy_forgot_password_explains_limitation_without_wiping_data(self):
        self.make_vault(recoverable=False)
        before, data = self.settings_bytes(), self.data_bytes()
        self.app.vault.clear_vault_key()
        with patch.object(self.app.messagebox, "showinfo") as info:

            def driver():
                self.widget("forgot_password").invoke()
                info.assert_called_once()
                self.assertIn("not enabled", info.call_args.args[1])
                self.widget("exit_vault").invoke()

            self.assertFalse(self.run_modal(lambda: self.app.show_vault_dialog(self.root), driver))
        self.assertEqual(self.settings_bytes(), before)
        self.assertEqual(self.data_bytes(), data)

    def test_failed_reset_returns_controls_without_changing_data(self):
        self.make_vault()
        before = self.settings_bytes()
        with (
            patch.object(
                self.app.vault,
                "reset_vault_password_locally",
                side_effect=self.app.vault.VaultError("Injected reset failure"),
            ),
            patch.object(self.app.messagebox, "showerror") as error,
        ):

            def after_failure():
                error.assert_called_once()
                self.assertEqual(self.widget("submit_reset").cget("state"), "normal")
                self.assertIn("RESET FAILED", self.widget("reset_status").cget("text"))
                self.widget("cancel_reset").invoke()

            def driver():
                self.fill_reset()
                self.widget("submit_reset").invoke()
                self.root.after(150, self.guarded(after_failure))

            self.assertFalse(
                self.run_modal(lambda: self.app.show_password_reset_dialog(self.root), driver)
            )
        self.assertEqual(self.settings_bytes(), before)

    def test_busy_reset_keeps_tk_responsive_and_prevents_duplicate_submission(self):
        self.make_vault()
        released = threading.Event()
        self.addCleanup(released.set)
        original = self.app.vault.reset_vault_password_locally
        heartbeat = []

        def slow_reset(password):
            if not released.wait(timeout=4):
                raise self.app.vault.VaultError("Test release was not received")
            return original(password)

        with patch.object(
            self.app.vault, "reset_vault_password_locally", side_effect=slow_reset
        ) as reset:

            def while_busy():
                heartbeat.append(True)
                self.assertEqual(self.widget("submit_reset").cget("state"), "disabled")
                self.assertEqual(self.widget("cancel_reset").cget("state"), "disabled")
                self.widget("submit_reset").invoke()
                self.widget("password_reset").event_generate("<Escape>")
                self.assertTrue(self.widget("password_reset").winfo_exists())
                released.set()

            def driver():
                self.fill_reset()
                self.widget("submit_reset").invoke()
                self.root.after(100, self.guarded(while_busy))

            self.assertTrue(
                self.run_modal(lambda: self.app.show_password_reset_dialog(self.root), driver)
            )
            reset.assert_called_once()
        self.assertEqual(heartbeat, [True])


if __name__ == "__main__":
    unittest.main()
