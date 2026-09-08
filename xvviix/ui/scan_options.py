"""Explicit scope selection for local software discovery."""

import tkinter as tk
from .lifecycle import DialogVariable
from ..constants import BG, CARD2, TEXT, SUBTEXT, ACCENT, GREEN


def choose_scan_scope(parent, default="registered"):
    result = {"scope": None}
    window = tk.Toplevel(parent, name="scan_scope")
    window.title("Choose scan scope")
    window.configure(bg=BG, padx=24, pady=22)
    window.transient(parent)
    window.resizable(False, False)
    previous_grab = parent.grab_current()
    choice = DialogVariable(
        window, default if default in {"registered", "search"} else "registered"
    )
    tk.Label(
        window, text="WHERE SHOULD XVVIIX LOOK?", bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold")
    ).pack(anchor="w", pady=(0, 12))
    descriptions = (
        (
            "registered",
            "CONTROL PANEL — REGISTERED APPS",
            "Launchable registered applications, excluding runtimes, SDKs and helper components.",
        ),
        (
            "search",
            "FIND PROGRAMS — USUAL INSTALL LOCATIONS",
            "Program Files, user Programs, Start Menu and known application install folders. No whole-drive scan.",
        ),
    )
    for key, title, detail in descriptions:
        tk.Radiobutton(
            window,
            name="scope_" + key,
            text=title,
            variable=choice,
            value=key,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            activeforeground=TEXT,
            selectcolor=CARD2,
            font=("Consolas", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(6, 0))
        tk.Label(
            window,
            text=detail,
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        ).pack(fill="x", padx=(24, 0), pady=(1, 5))
    tk.Label(
        window,
        text="Only two local modes. No whole-system/drive sweep. Embedded tools and duplicate product versions are filtered; uncertain items remain for review. No program is executed or deleted.",
        bg=BG,
        fg=SUBTEXT,
        wraplength=480,
        justify="left",
        font=("Segoe UI", 9),
    ).pack(fill="x", pady=(12, 16))
    row = tk.Frame(window, bg=BG)
    row.pack(fill="x")

    def accept():
        result["scope"] = choice.get()
        window.destroy()

    tk.Button(
        row,
        name="cancel_scope",
        text="CANCEL",
        command=window.destroy,
        bg=CARD2,
        fg=TEXT,
        relief="flat",
        padx=18,
        pady=8,
    ).pack(side="left")
    tk.Button(
        row,
        name="start_scope",
        text="START SCAN",
        command=accept,
        bg=GREEN,
        fg=TEXT,
        activebackground=ACCENT,
        relief="flat",
        padx=20,
        pady=8,
    ).pack(side="right")
    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.bind("<Escape>", lambda _event: window.destroy())
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    window.geometry(
        f"{width}x{height}+{max(0, (parent.winfo_screenwidth() - width) // 2)}+{max(0, (parent.winfo_screenheight() - height) // 2)}"
    )
    window.grab_set()
    parent.wait_window(window)
    try:
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
    except tk.TclError:
        pass
    return result["scope"]
