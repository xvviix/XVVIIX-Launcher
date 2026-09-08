"""Two fixed-height top-five process panels, not a full task-manager table."""

import tkinter as tk
from tkinter import ttk

from ..constants import BG2, BORDER, CARD, CARD2, CYAN, GREEN_HOVER, MUTED, TEXT
from ..services.hardware_monitor import (
    TOP_PROCESS_LIMIT,
    monitor_clamp_percent,
    monitor_format_bytes,
)


class TopProcessPanels:
    def __init__(self, parent):
        self.container = tk.Frame(
            parent, bg=CARD, padx=16, pady=13, highlightbackground=BORDER, highlightthickness=1
        )
        self.container.grid_columnconfigure(0, weight=1, uniform="top-processes")
        self.container.grid_columnconfigure(1, weight=1, uniform="top-processes")
        self.trees = {}
        self._rows = {"cpu": {}, "memory": {}}
        self._positions = {"cpu": {}, "memory": {}}
        self._status = None
        style = ttk.Style(parent)
        style.configure(
            "XVVIIXTop.Treeview",
            background=CARD2,
            fieldbackground=CARD2,
            foreground=TEXT,
            rowheight=25,
            borderwidth=0,
            font=("Segoe UI", 8),
        )
        style.configure(
            "XVVIIXTop.Treeview.Heading",
            background=BG2,
            foreground=TEXT,
            relief="flat",
            font=("Consolas", 8, "bold"),
        )
        style.map(
            "XVVIIXTop.Treeview", background=[("selected", CARD2)], foreground=[("selected", TEXT)]
        )
        for column, kind, label, accent in (
            (0, "cpu", "TOP 5 · CPU", CYAN),
            (1, "memory", "TOP 5 · RAM", GREEN_HOVER),
        ):
            panel = tk.Frame(self.container, bg=CARD)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
            tk.Label(panel, text=label, bg=CARD, fg=accent, font=("Consolas", 10, "bold")).pack(
                anchor="w", pady=(0, 8)
            )
            tree = ttk.Treeview(
                panel,
                columns=("name", "usage"),
                show="headings",
                height=TOP_PROCESS_LIMIT,
                style="XVVIIXTop.Treeview",
                selectmode="none",
            )
            tree.heading("name", text="PROCESS")
            tree.heading("usage", text="CPU" if kind == "cpu" else "RAM")
            tree.column("name", width=220, minwidth=100, stretch=True, anchor="w")
            tree.column(
                "usage", width=70 if kind == "cpu" else 135, minwidth=65, stretch=False, anchor="e"
            )
            tree.tag_configure("even", background=CARD2, foreground=TEXT)
            tree.tag_configure("odd", background="#111c31", foreground=TEXT)
            tree.pack(fill="both", expand=True)
            self.trees[kind] = tree
        self.status = tk.Label(
            self.container,
            text="Waiting for top-process sample",
            bg=CARD,
            fg=MUTED,
            font=("Consolas", 8),
            anchor="w",
        )
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def update(self, top_cpu, top_memory, eligible_count=0):
        for kind, records in (("cpu", top_cpu), ("memory", top_memory)):
            tree = self.trees[kind]
            previous, positions = self._rows[kind], self._positions[kind]
            rows, ordering = {}, []
            for index, record in enumerate(list(records or [])[:TOP_PROCESS_LIMIT]):
                pid = int(record.get("pid") or 0)
                iid = f"{kind}-{pid}" if pid > 0 else f"{kind}-row-{index}"
                if iid in rows:
                    continue
                name = " ".join(str(record.get("name") or "Unknown").split())[:80]
                field = "cpu_percent" if kind == "cpu" else "memory_percent"
                percent = monitor_clamp_percent(record.get(field)) or 0.0
                usage = f"{percent:.1f}%"
                if kind == "memory":
                    usage = f"{monitor_format_bytes(record.get('memory_bytes'))} · {percent:.1f}%"
                values, tag = (name, usage), "even" if index % 2 == 0 else "odd"
                signature = (values, tag)
                exists = tree.exists(iid)
                if not exists:
                    tree.insert("", "end", iid=iid, values=values, tags=(tag,))
                elif previous.get(iid) != signature:
                    tree.item(iid, values=values, tags=(tag,))
                if not exists or positions.get(iid) != index:
                    tree.move(iid, "", index)
                rows[iid] = signature
                ordering.append(iid)
            obsolete = set(tree.get_children()) - set(ordering)
            if obsolete:
                tree.delete(*obsolete)
            self._rows[kind] = rows
            self._positions[kind] = {iid: index for index, iid in enumerate(ordering)}
        text = f"CURRENT USER · {max(0, int(eligible_count or 0))} ELIGIBLE PROCESSES · TOP 5 EACH · REFRESH ~3s"
        if text != self._status:
            self.status.configure(text=text)
            self._status = text
