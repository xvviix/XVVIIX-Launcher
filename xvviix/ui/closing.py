"""Responsive, bounded-user-choice shutdown while final data is saved."""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..constants import BG, CARD2, TEXT, SUBTEXT, RED, GREEN


class ClosingDialog:
    def __init__(self, parent, save, complete):
        self.save = save
        self.complete = complete
        self.outcomes = queue.Queue()
        self.busy = False
        self.closed = False
        self.window = tk.Toplevel(parent, name="closing_launcher")
        self.window.title("Closing XVVIIX")
        self.window.configure(bg=BG, padx=24, pady=20)
        self.window.resizable(False, False)
        self.window.transient(parent)
        tk.Label(
            self.window, text="SAVING YOUR SESSION", bg=BG, fg=TEXT, font=("Segoe UI", 14, "bold")
        ).pack(anchor="w")
        self.status = tk.Label(
            self.window,
            text="Saving the last playtime checkpoint…\nYour games will stay open.",
            bg=BG,
            fg=SUBTEXT,
            justify="left",
            wraplength=420,
            font=("Segoe UI", 10),
        )
        self.status.pack(fill="x", pady=(10, 12))
        self.progress = ttk.Progressbar(self.window, mode="indeterminate", length=410)
        self.progress.pack(fill="x", pady=(0, 14))
        buttons = tk.Frame(self.window, bg=BG)
        buttons.pack(fill="x")
        self.retry = tk.Button(
            buttons,
            text="RETRY SAVE",
            command=self.begin,
            state="disabled",
            bg=GREEN,
            fg=TEXT,
            relief="flat",
            padx=14,
            pady=7,
        )
        self.retry.pack(side="left")
        self.force = tk.Button(
            buttons,
            text="EXIT WITHOUT LATEST SAVE",
            command=self.force_exit,
            state="disabled",
            bg=CARD2,
            fg=TEXT,
            relief="flat",
            padx=14,
            pady=7,
        )
        self.force.pack(side="right")
        self.window.protocol("WM_DELETE_WINDOW", self.force_exit)
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = max(0, (parent.winfo_screenwidth() - width) // 2)
        y = max(0, (parent.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.grab_set()
        self.begin()
        self.window.after(40, self.poll)
        self.window.after(3500, self.allow_force)

    def begin(self):
        if self.closed or self.busy:
            return
        self.busy = True
        self.retry.configure(state="disabled")
        self.status.configure(
            text="Saving the last playtime checkpoint…\nYour games will stay open.", fg=SUBTEXT
        )
        self.progress.start(12)

        def worker():
            try:
                problems = self.save() or []
                self.outcomes.put((not problems, "; ".join(problems)))
            except Exception as exc:
                self.outcomes.put((False, str(exc)))

        threading.Thread(target=worker, daemon=True, name="launcher-final-save").start()

    def allow_force(self):
        if not self.closed:
            self.force.configure(state="normal")
            if self.busy:
                self.status.configure(
                    text="The final save is still running. The interface is responsive.\nYou can wait, or explicitly exit without the latest timing changes."
                )

    def poll(self):
        if self.closed:
            return
        try:
            success, detail = self.outcomes.get_nowait()
        except queue.Empty:
            self.window.after(40, self.poll)
            return
        self.busy = False
        self.progress.stop()
        if success:
            self.closed = True
            self.complete(False)
            return
        self.status.configure(text="Could not complete the final save.\n" + detail[:500], fg=RED)
        self.retry.configure(state="normal")
        self.force.configure(state="normal")
        self.window.after(40, self.poll)

    def force_exit(self):
        if self.closed:
            return
        if not messagebox.askyesno(
            "Exit without latest save?",
            "The latest playtime may not be saved. Existing saved data will not be deliberately erased.\n\nExit the launcher anyway? Your games will stay open.",
            parent=self.window,
            icon="warning",
        ):
            return
        self.closed = True
        self.complete(True)
