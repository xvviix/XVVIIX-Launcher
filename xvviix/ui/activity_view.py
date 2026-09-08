"""Live, filterable activity history independent of the library-card renderer."""

from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk

from .lifecycle import DialogVariable
from ..constants import BG, BG2, CARD, CARD2, TEXT, SUBTEXT, NEON, MUTED, ACCENT


def activity_age(epoch, now=None):
    try:
        seconds = max(0, int((time.time() if now is None else now) - float(epoch)))
    except (TypeError, ValueError, OverflowError):
        return "RECENT"
    if seconds < 5:
        return "JUST NOW"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def activity_category(kind):
    if kind in {"launch", "detected", "closed", "end_task"}:
        return "programs"
    if kind in {"library", "icon", "location"}:
        return "library"
    if kind in {"scan", "report"}:
        return "discovery"
    return "other"


class ActivityView:
    def __init__(self, parent, records, clear_history, *, on_close=None):
        self.records = records
        self.clear_history = clear_history
        self.on_close = on_close
        self.closed = False
        self.job = None
        self._signature = None
        self._records = {}
        self._rendered = {}
        self.window = tk.Toplevel(parent, name="recent_activity_history")
        self.window.title("Recent activity")
        self.window.configure(bg=BG, padx=18, pady=16)
        self.window.geometry("780x480")
        self.window.minsize(620, 380)
        self.window.transient(parent)
        tk.Label(
            self.window, text="RECENT ACTIVITY", bg=BG, fg=TEXT, font=("Segoe UI", 16, "bold")
        ).pack(anchor="w")
        tk.Label(
            self.window,
            text="Local history · timestamps, outcomes and library changes",
            bg=BG,
            fg=SUBTEXT,
            font=("Consolas", 9),
        ).pack(anchor="w", pady=(3, 12))
        controls = tk.Frame(self.window, bg=BG)
        controls.pack(fill="x", pady=(0, 10))
        self.query = DialogVariable(self.window, "")
        self.category = DialogVariable(self.window, "all")
        self.search = tk.Entry(
            controls,
            textvariable=self.query,
            bg=BG2,
            fg=TEXT,
            insertbackground=NEON,
            relief="flat",
            font=("Segoe UI", 10),
        )
        self.search.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.filter = tk.OptionMenu(
            controls, self.category, "all", "programs", "library", "discovery", "other"
        )
        self.filter.configure(
            bg=CARD2,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=12,
            pady=5,
        )
        self.filter["menu"].configure(
            bg=CARD2, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT
        )
        self.filter.pack(side="left", padx=(0, 8))
        self.clear_button = tk.Button(
            controls,
            text="CLEAR HISTORY",
            command=self._clear,
            bg=CARD2,
            fg=TEXT,
            activebackground=ACCENT,
            relief="flat",
            padx=10,
            pady=6,
        )
        self.clear_button.pack(side="right")
        self.query.trace_add("write", lambda *_args: self.refresh(force=True))
        self.category.trace_add("write", lambda *_args: self.refresh(force=True))
        headings = tk.Frame(self.window, bg=BG2, padx=1, pady=7)
        headings.pack(fill="x")
        headings.grid_columnconfigure(0, minsize=155, weight=0)
        headings.grid_columnconfigure(1, minsize=260, weight=1)
        headings.grid_columnconfigure(2, minsize=200, weight=1)
        for column, title in enumerate(("TIME", "EVENT", "PROGRAM / ITEM")):
            tk.Label(
                headings,
                text=title,
                bg=BG2,
                fg=NEON,
                anchor="w",
                padx=6,
                font=("Consolas", 9, "bold"),
            ).grid(row=0, column=column, sticky="ew")
        body = tk.Frame(self.window, bg=CARD)
        body.pack(fill="both", expand=True)
        style = ttk.Style(self.window)
        style.configure(
            "XVVIIXActivity.Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=27,
            font=("Segoe UI", 9),
            borderwidth=0,
        )
        self.tree = ttk.Treeview(
            body,
            columns=("time", "event", "item"),
            show="",
            style="XVVIIXActivity.Treeview",
            selectmode="browse",
        )
        for key, title, width in (
            ("time", "TIME", 155),
            ("event", "EVENT", 260),
            ("item", "PROGRAM / ITEM", 200),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=100, stretch=key != "time", anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("warning", foreground="#fbbf24")
        self.tree.tag_configure("error", foreground="#fb7185")
        self.tree.tag_configure("success", foreground="#5eead4")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._detail())
        self.tree.bind("<MouseWheel>", self._wheel)
        self.detail = tk.Label(
            self.window,
            text="Select an event for details.",
            bg=BG,
            fg=SUBTEXT,
            font=("Consolas", 9),
            justify="left",
            anchor="w",
            wraplength=730,
        )
        self.detail.pack(fill="x", pady=(12, 0))
        self.count = tk.Label(
            self.window, text="", bg=BG, fg=MUTED, font=("Consolas", 8), anchor="w"
        )
        self.count.pack(fill="x", pady=(7, 0))
        self.window.protocol("WM_DELETE_WINDOW", self.destroy)
        self.window.bind("<Destroy>", self._destroyed, add="+")
        self.window.bind("<Escape>", lambda _event: self.destroy())
        self.refresh(force=True)
        self.job = self.window.after(1000, self._tick)

    def _wheel(self, event):
        self.tree.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _tick(self):
        self.job = None
        if self.closed:
            return
        if self.window.winfo_viewable():
            self.refresh()
        self.job = self.window.after(1000, self._tick)

    def refresh(self, force=False):
        if self.closed:
            return
        records = [row for row in self.records() if row.get("kind") != "crash"]
        query = self.query.get().strip().casefold()
        category = self.category.get()
        visible = [
            row
            for row in records
            if (category == "all" or activity_category(row.get("kind")) == category)
            and (
                not query
                or query
                in " ".join(
                    str(row.get(key, "")) for key in ("title", "item_name", "detail", "kind")
                ).casefold()
            )
        ]
        signature = (
            query,
            category,
            tuple(
                (row.get("id"), row.get("epoch"), row.get("title"), row.get("detail"))
                for row in visible
            ),
        )
        if signature == self._signature and not force:
            self._detail()
            return
        selected = self.tree.selection()
        scroll = self.tree.yview()
        previous_order = self.tree.get_children()
        first_index = min(
            len(previous_order) - 1,
            int((scroll[0] if scroll else 0) * len(previous_order) + 0.00001),
        )
        top = previous_order[first_index] if previous_order else ""
        following_top = not scroll or scroll[0] < 0.01
        expected = []
        record_map = {}
        for index, row in enumerate(visible):
            iid = str(row.get("id") or f"activity-{index}")
            if iid in record_map:
                continue
            epoch = row.get("epoch", 0)
            try:
                timestamp = datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, OSError, OverflowError):
                timestamp = "Time unavailable"
            values = (
                timestamp,
                str(row.get("title") or "Activity"),
                str(row.get("item_name") or "—"),
            )
            tag = str(row.get("severity") or "info")
            if not self.tree.exists(iid):
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
            elif self._rendered.get(iid) != (values, tag):
                self.tree.item(iid, values=values, tags=(tag,))
            self.tree.move(iid, "", index)
            self._rendered[iid] = (values, tag)
            expected.append(iid)
            record_map[iid] = row
        stale = set(self.tree.get_children()) - set(expected)
        if stale:
            self.tree.delete(*stale)
        self._rendered = {key: value for key, value in self._rendered.items() if key in record_map}
        self._records = record_map
        if selected and selected[0] in record_map:
            self.tree.selection_set(selected[0])
        if not following_top and top in expected:
            self.tree.yview_moveto(0)
            self.tree.yview_scroll(expected.index(top), "units")
        elif following_top:
            self.tree.yview_moveto(0)
        self.count.configure(
            text=f"{len(visible)} visible / {len(records)} recent events · history only, not program files"
        )
        self.clear_button.configure(state="normal" if records else "disabled")
        self._signature = signature
        self._detail()

    def _detail(self):
        selected = self.tree.selection()
        record = self._records.get(selected[0]) if selected else None
        if record is None:
            self.detail.configure(text="Select an event for details.")
        else:
            text = f"{record.get('title', 'Activity')} · {activity_age(record.get('epoch'))}\n{record.get('detail') or record.get('item_path') or 'No additional details.'}"
            self.detail.configure(text=text[:1400])

    def _clear(self):
        if self.clear_history():
            self.refresh(force=True)

    def _destroyed(self, event):
        if event.widget is self.window:
            self.closed = True
            self._cancel()

    def _cancel(self):
        if self.job is not None:
            try:
                self.window.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        self._cancel()
        self.window.destroy()
        if self.on_close is not None:
            self.on_close()
