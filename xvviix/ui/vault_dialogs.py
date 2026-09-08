"""Vault dialogs. All data/cryptography operations go through an injected VaultStore."""

from dataclasses import dataclass
import json
import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox
from .. import constants
from .lifecycle import DialogVariable


@dataclass(frozen=True)
class VaultTheme:
    ACCENT: str = constants.ACCENT
    ACCENT2: str = constants.ACCENT2
    BG: str = constants.BG
    BG2: str = constants.BG2
    BORDER: str = constants.BORDER
    CARD: str = constants.CARD
    CARD2: str = constants.CARD2
    GREEN: str = constants.GREEN
    MUTED: str = constants.MUTED
    NEON: str = constants.NEON
    ORANGE: str = constants.ORANGE
    RED: str = constants.RED
    SUBTEXT: str = constants.SUBTEXT
    TEXT: str = constants.TEXT


def _widget_exists(widget):
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


class VaultDialogs:
    """Modal UI with explicit storage, presentation and completion dependencies."""

    def __init__(
        self, store, *, load_data, theme=None, animate_button=None, round_corners=None, logger=None
    ):
        self.store = store
        self._load_data = load_data
        self.theme = theme if theme is not None else VaultTheme()
        self._animate_button = (
            animate_button if animate_button is not None else lambda *args, **kwargs: None
        )
        self._round_corners = round_corners if round_corners is not None else lambda window: False
        self.LOG = logger if logger is not None else logging.getLogger("xvviix_launcher")

    def _position_vault_dialog(self, dialog, width=560):
        """Fit modal content instead of clipping controls at a fixed pixel height."""
        dialog.update_idletasks()
        width = max(width, dialog.winfo_reqwidth())
        height = dialog.winfo_reqheight()
        x = max(0, (dialog.winfo_screenwidth() - width) // 2)
        y = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        self._round_corners(dialog)

    def show_password_reset_dialog(self, parent):
        """Ask only for a new password; recovery is local, with no email or manual code."""
        available, reason = self.store.local_password_reset_status()
        if not available:
            messagebox.showinfo("Password reset unavailable", reason, parent=parent)
            return False

        result = {"reset": False, "busy": False}
        outcomes = queue.Queue()
        previous_grab = parent.grab_current()
        dialog = tk.Toplevel(parent, name="password_reset")
        dialog.withdraw()
        dialog.title("XVVIIX — Forgot Password")
        dialog.configure(bg=self.theme.BG)
        dialog.resizable(False, False)
        dialog.transient(parent)
        tk.Frame(dialog, bg=self.theme.NEON, height=4).pack(fill="x")
        body = tk.Frame(dialog, bg=self.theme.BG, padx=28, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="RESET YOUR PASSWORD",
            bg=self.theme.BG,
            fg=self.theme.TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text="LOCAL RECOVERY  /  NO CODE REQUIRED",
            bg=self.theme.BG,
            fg=self.theme.NEON,
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(5, 14))
        tk.Label(
            body,
            text="Choose a new password. Your games, apps, reports and activity will be kept. "
            "The old password is not required and will not be shown.",
            bg=self.theme.BG,
            fg=self.theme.SUBTEXT,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=500,
        ).pack(anchor="w", pady=(0, 16))

        new_password = DialogVariable(dialog)
        confirmation = DialogVariable(dialog)
        entries = []
        for label, variable, name in (
            ("NEW PASSWORD", new_password, "new_password"),
            ("CONFIRM NEW PASSWORD", confirmation, "confirm_password"),
        ):
            tk.Label(
                body,
                text=label,
                bg=self.theme.BG,
                fg=self.theme.MUTED,
                font=("Segoe UI", 8, "bold"),
            ).pack(anchor="w", pady=(8, 4))
            entry = tk.Entry(
                body,
                name=name,
                textvariable=variable,
                show="●",
                bg=self.theme.BG2,
                fg=self.theme.TEXT,
                insertbackground=self.theme.NEON,
                relief="flat",
                highlightbackground=self.theme.BORDER,
                highlightcolor=self.theme.NEON,
                highlightthickness=1,
                font=("Segoe UI", 11),
            )
            entry.pack(fill="x", ipady=8)
            entries.append(entry)

        tk.Label(
            body,
            text="CONVENIENCE LOCK: anyone using this OS account with access to the local "
            "recovery file can set a new password. Keep that file private.",
            bg=self.theme.CARD,
            fg=self.theme.ORANGE,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=470,
            padx=12,
            pady=10,
        ).pack(fill="x", pady=(18, 6))
        status = tk.Label(
            body,
            name="reset_status",
            text="MINIMUM 8 CHARACTERS",
            bg=self.theme.BG,
            fg=self.theme.MUTED,
            font=("Consolas", 8, "bold"),
            anchor="w",
            justify="left",
            wraplength=500,
        )
        status.pack(fill="x", pady=(8, 14))
        actions = tk.Frame(body, bg=self.theme.BG)
        actions.pack(fill="x")

        def close():
            if result["busy"]:
                return
            new_password.set("")
            confirmation.set("")
            dialog.grab_release()
            dialog.destroy()

        def poll_result():
            try:
                successful, error = outcomes.get_nowait()
            except queue.Empty:
                dialog.after(50, poll_result)
                return
            result["busy"] = False
            if successful:
                result["reset"] = True
                close()
                return
            for entry in entries:
                entry.config(state="normal")
            reset_button.config(state="normal")
            cancel_button.config(state="normal")
            status.config(text="RESET FAILED — REVIEW THE MESSAGE AND TRY AGAIN", fg=self.theme.RED)
            messagebox.showerror("Password reset failed", error, parent=dialog)
            entries[0].focus_set()

        def submit():
            if result["busy"]:
                return
            password = new_password.get()
            if not 8 <= len(password) <= 1024:
                status.config(text="PASSWORD MUST CONTAIN 8 TO 1024 CHARACTERS", fg=self.theme.RED)
                return
            if password != confirmation.get():
                status.config(text="PASSWORDS DO NOT MATCH", fg=self.theme.RED)
                return
            result["busy"] = True
            status.config(text="VERIFYING DATA AND UPDATING PASSWORD...", fg=self.theme.ORANGE)
            for entry in entries:
                entry.config(state="disabled")
            reset_button.config(state="disabled")
            cancel_button.config(state="disabled")
            new_password.set("")
            confirmation.set("")

            def worker(value):
                successful, error = False, ""
                try:
                    self.store.reset_vault_password_locally(value)
                    successful = True
                except Exception as exc:
                    error = str(exc) or "Password reset could not be completed."
                    self.LOG.warning("Local password reset failed: %s", error)
                finally:
                    value = ""
                outcomes.put((successful, error))

            try:
                threading.Thread(
                    target=worker,
                    args=(password,),
                    daemon=True,
                    name="vault-password-reset",
                ).start()
            except RuntimeError as exc:
                outcomes.put((False, str(exc)))
            finally:
                password = ""
            dialog.after(50, poll_result)

        cancel_button = tk.Button(
            actions,
            name="cancel_reset",
            text="CANCEL",
            bg=self.theme.CARD2,
            fg=self.theme.SUBTEXT,
            relief="flat",
            bd=0,
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            command=close,
        )
        cancel_button.pack(side="left")
        reset_button = tk.Button(
            actions,
            name="submit_reset",
            text="SAVE PASSWORD & UNLOCK",
            bg=self.theme.ACCENT,
            fg=self.theme.TEXT,
            relief="flat",
            bd=0,
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            command=submit,
        )
        reset_button.pack(side="right")
        self._animate_button(
            cancel_button, self.theme.CARD2, self.theme.RED, self.theme.SUBTEXT, self.theme.TEXT
        )
        self._animate_button(
            reset_button, self.theme.ACCENT, self.theme.ACCENT2, self.theme.TEXT, self.theme.TEXT
        )
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", lambda _event: close())
        dialog.bind("<Return>", lambda _event: submit())
        self._position_vault_dialog(dialog)
        dialog.deiconify()
        dialog.grab_set()
        entries[0].focus_set()
        try:
            parent.wait_window(dialog)
        finally:
            if previous_grab is not None and _widget_exists(previous_grab):
                previous_grab.grab_set()
        return result["reset"]

    def show_vault_dialog(self, parent, setup=False):
        """Password setup/unlock; local reset is explicit opt-in for existing vaults."""
        result = {"unlocked": False, "busy": False}
        reset_available = False if setup else self.store.local_password_reset_status()[0]
        offer_local_reset = setup or not reset_available
        dialog = tk.Toplevel(parent, name="vault_dialog")
        dialog.withdraw()
        dialog.title("XVVIIX Data Vault")
        dialog.configure(bg=self.theme.BG)
        dialog.resizable(False, False)
        tk.Frame(dialog, bg=self.theme.ACCENT, height=4).pack(fill="x")
        body = tk.Frame(dialog, bg=self.theme.BG, padx=32, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="◆  XVVIIX DATA VAULT",
            bg=self.theme.BG,
            fg=self.theme.TEXT,
            font=("Segoe UI Black", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text="CREATE A MASTER PASSWORD" if setup else "ENCRYPTED LIBRARIES DETECTED",
            bg=self.theme.BG,
            fg=self.theme.NEON,
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(5, 18))
        tk.Label(
            body,
            text=(
                "Games, Workspace, Discovered, Reports, and Activity will be encrypted. "
                "Choose whether to allow code-free password reset on this computer."
                if setup
                else "Enter your master password to decrypt the launcher data for this session."
            ),
            bg=self.theme.BG,
            fg=self.theme.SUBTEXT,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=495,
        ).pack(anchor="w", pady=(0, 15))

        password_var = DialogVariable(dialog)
        confirm_var = DialogVariable(dialog)
        local_reset_var = DialogVariable(dialog, value=setup, boolean=True)
        tk.Label(
            body,
            text="MASTER PASSWORD",
            bg=self.theme.BG,
            fg=self.theme.MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        password_entry = tk.Entry(
            body,
            name="master_password",
            textvariable=password_var,
            show="●",
            bg=self.theme.BG2,
            fg=self.theme.TEXT,
            insertbackground=self.theme.NEON,
            relief="flat",
            highlightbackground=self.theme.BORDER,
            highlightcolor=self.theme.NEON,
            highlightthickness=1,
            font=("Segoe UI", 11),
        )
        password_entry.pack(fill="x", ipady=8)

        if setup:
            tk.Label(
                body,
                text="CONFIRM PASSWORD",
                bg=self.theme.BG,
                fg=self.theme.MUTED,
                font=("Segoe UI", 8, "bold"),
            ).pack(anchor="w", pady=(12, 4))
            confirm_entry = tk.Entry(
                body,
                name="confirm_password",
                textvariable=confirm_var,
                show="●",
                bg=self.theme.BG2,
                fg=self.theme.TEXT,
                insertbackground=self.theme.NEON,
                relief="flat",
                highlightbackground=self.theme.BORDER,
                highlightcolor=self.theme.NEON,
                highlightthickness=1,
                font=("Segoe UI", 11),
            )
            confirm_entry.pack(fill="x", ipady=8)

        if offer_local_reset:
            tk.Checkbutton(
                body,
                name="local_reset_opt_in",
                text="Enable local password reset on this computer",
                variable=local_reset_var,
                bg=self.theme.BG,
                fg=self.theme.TEXT,
                selectcolor=self.theme.CARD2,
                activebackground=self.theme.BG,
                activeforeground=self.theme.NEON,
                highlightthickness=0,
                font=("Segoe UI", 9),
                anchor="w",
                padx=0,
            ).pack(fill="x", pady=(14, 3))
            tk.Label(
                body,
                text="No code required. Anyone using this OS account with the recovery file "
                "can change your password. "
                + (
                    "Leave this unchecked for password-only protection."
                    if setup
                    else "For an existing vault, sign in once to enable this option."
                ),
                bg=self.theme.BG,
                fg=self.theme.ORANGE,
                font=("Segoe UI", 9),
                justify="left",
                wraplength=495,
            ).pack(anchor="w", pady=(0, 5))
        else:
            tk.Label(
                body,
                text="LOCAL PASSWORD RESET IS AVAILABLE ON THIS COMPUTER",
                bg=self.theme.BG,
                fg=self.theme.GREEN,
                font=("Consolas", 8, "bold"),
            ).pack(anchor="w", pady=(12, 2))

        status_label = tk.Label(
            body,
            name="vault_status",
            text="MINIMUM 8 CHARACTERS  ·  USE A UNIQUE PASSPHRASE"
            if setup
            else "AES-256-GCM  ·  SCRYPT KEY DERIVATION",
            bg=self.theme.BG,
            fg=self.theme.MUTED,
            font=("Consolas", 8, "bold"),
            anchor="w",
        )
        status_label.pack(fill="x", pady=(12, 8))

        def cancel():
            if result["busy"]:
                return
            password_var.set("")
            confirm_var.set("")
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

        def finish_unlock():
            self._load_data()
            result["unlocked"] = True
            result["busy"] = False
            cancel()

        def forgot_password():
            if result["busy"]:
                return
            result["busy"] = True
            try:
                if self.show_password_reset_dialog(dialog):
                    finish_unlock()
            except (self.store.VaultError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.store.clear_vault_key()
                messagebox.showerror("XVVIIX Data Vault", str(exc), parent=dialog)
            finally:
                result["busy"] = False

        if not setup:
            forgot_button = tk.Button(
                body,
                name="forgot_password",
                text="FORGOT PASSWORD?",
                bg=self.theme.BG,
                fg=self.theme.ACCENT2,
                activebackground=self.theme.BG,
                activeforeground=self.theme.NEON,
                relief="flat",
                bd=0,
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
                command=forgot_password,
            )
            forgot_button.pack(anchor="e", pady=(0, 12))

        actions = tk.Frame(body, bg=self.theme.BG)
        actions.pack(fill="x", pady=(6, 0))

        def submit():
            if result["busy"]:
                return
            password = password_var.get()
            if setup:
                if not 8 <= len(password) <= 1024:
                    status_label.config(
                        text="PASSWORD MUST CONTAIN 8 TO 1024 CHARACTERS", fg=self.theme.RED
                    )
                    return
                if password != confirm_var.get():
                    status_label.config(text="PASSWORDS DO NOT MATCH", fg=self.theme.RED)
                    return
            result["busy"] = True
            status_label.config(text="DERIVING KEY AND SECURING DATA...", fg=self.theme.ORANGE)
            submit_button.config(state="disabled")
            dialog.update_idletasks()
            try:
                if setup:
                    self.store.create_data_vault(password, allow_local_reset=local_reset_var.get())
                else:
                    self.store.unlock_data_vault(password)
                    if local_reset_var.get():
                        self.store.enable_local_password_reset(password)
                finish_unlock()
            except self.store.VaultPasswordError:
                self.store.clear_vault_key()
                status_label.config(text="INCORRECT PASSWORD", fg=self.theme.RED)
                password_entry.selection_range(0, tk.END)
                password_entry.focus_set()
                submit_button.config(state="normal")
            except (self.store.VaultError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.store.clear_vault_key()
                metadata_committed = setup and os.path.isfile(self.store.VAULT_FILE)
                status_label.config(
                    text="RESTART XVVIIX TO RESUME VAULT SETUP"
                    if metadata_committed
                    else "VAULT ERROR — SEE MESSAGE",
                    fg=self.theme.RED,
                )
                submit_button.config(state="disabled" if metadata_committed else "normal")
                messagebox.showerror("XVVIIX Data Vault", str(exc), parent=dialog)
            finally:
                password = ""
                result["busy"] = False

        cancel_button = tk.Button(
            actions,
            name="exit_vault",
            text="EXIT",
            bg=self.theme.CARD2,
            fg=self.theme.SUBTEXT,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=20,
            pady=9,
            cursor="hand2",
            bd=0,
            command=cancel,
        )
        cancel_button.pack(side="left")
        submit_button = tk.Button(
            actions,
            name="submit_vault",
            text="CREATE VAULT" if setup else "UNLOCK XVVIIX",
            bg=self.theme.ACCENT,
            fg=self.theme.TEXT,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=22,
            pady=9,
            cursor="hand2",
            bd=0,
            command=submit,
        )
        submit_button.pack(side="right")
        self._animate_button(
            cancel_button, self.theme.CARD2, self.theme.RED, self.theme.SUBTEXT, self.theme.TEXT
        )
        self._animate_button(
            submit_button, self.theme.ACCENT, self.theme.ACCENT2, self.theme.TEXT, self.theme.TEXT
        )
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.bind("<Return>", lambda _event: submit())
        self._position_vault_dialog(dialog)
        dialog.deiconify()
        dialog.lift()
        dialog.grab_set()
        password_entry.focus_set()
        parent.wait_window(dialog)
        return result["unlocked"]

    def initialize_data_vault(self, parent):
        """Require setup or unlock before any protected library data is exposed."""
        if not self.store.HAS_CRYPTOGRAPHY:
            messagebox.showerror(
                "XVVIIX Data Vault",
                "Password protection requires the cryptography package.\n\n"
                "Run:  pip install -r requirements.txt",
                parent=parent,
            )
            return False
        metadata_exists = os.path.isfile(self.store.VAULT_FILE) or os.path.isfile(
            f"{self.store.VAULT_FILE}.bak"
        )
        if not metadata_exists and self.store.encrypted_data_exists_without_vault():
            messagebox.showerror(
                "XVVIIX Data Vault",
                "Encrypted library data exists, but xvviix_vault.json is missing.\n\n"
                "Restore xvviix_vault.json or xvviix_vault.json.bak before continuing.",
                parent=parent,
            )
            return False
        self.store.vault_enabled = metadata_exists
        return self.show_vault_dialog(parent, setup=not metadata_exists)
