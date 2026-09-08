"""Keyed library cards, stable scrolling, mirrored pagination and multi-selection."""

import tkinter as tk

from ..constants import BG, CARD2, TEXT, SUBTEXT, MUTED, NEON, ACCENT, RED


class CardHandle:
    def __init__(self, widget, update, dispose):
        self.widget = widget
        self.update = update
        self.dispose = dispose
        self.signature = None

    def destroy(self):
        if self.widget is not None:
            self.dispose(self.widget)
            self.widget.destroy()
        self.widget = self.update = self.dispose = None


class LibraryView:
    """Reuse each visible card unless that card's structural inputs actually change."""

    def __init__(self, parent, canvas, build_card, signature, on_page, on_action, *, background=BG):
        self.parent, self.canvas = parent, canvas
        self.build_card, self.signature = build_card, signature
        self.on_page, self.on_action = on_page, on_action
        self.background = background
        self.parent.configure(bg=background)
        self.canvas.configure(bg=background)
        self.cards = {}
        self.selected = set()
        self.all_items = []
        self.page_items = []
        self.context = None
        self.closed = False
        self._restore_job = None
        self._restore_expiry = None
        self._pending_anchor = None
        self._generation = 0
        self._configure_binding = parent.bind("<Configure>", self._layout_changed, add="+")
        self._wheel_bindings = [
            (event, canvas.bind(event, lambda _event: self._cancel_restore(), add="+"))
            for event in ("<MouseWheel>", "<Button-4>", "<Button-5>")
        ]
        self._positions = {}
        self._anchor_key = None
        self.created = self.rebuilt = self.live_updates = 0
        self.top = tk.Frame(parent, bg=background, name="library_top_controls")
        self.top_pager = self._pager(self.top, "top")
        self.top_pager["frame"].pack(fill="x", pady=(0, 6))
        self.selection_bar = tk.Frame(self.top, bg=background)
        self.selection_bar.pack(fill="x", pady=(0, 8))
        self.selection_label = tk.Label(
            self.selection_bar,
            text="0 SELECTED",
            bg=background,
            fg=MUTED,
            font=("Consolas", 9, "bold"),
        )
        self.selection_label.pack(side="left", padx=(8, 12))
        self.select_page_button = self._button(self.selection_bar, "SELECT PAGE", self.select_page)
        self.select_page_button.pack(side="left", padx=3)
        self.clear_button = self._button(self.selection_bar, "CLEAR", self.clear_selection)
        self.clear_button.pack(side="left", padx=3)
        self.actions_button = self._button(self.selection_bar, "ACTIONS ▾", self.show_actions)
        self.actions_button.pack(side="right", padx=3)
        self.delete_button = self._button(
            self.selection_bar, "REMOVE SELECTED", lambda: self.on_action("delete", None)
        )
        self.delete_button.configure(activebackground=RED)
        self.delete_button.pack(side="right", padx=3)
        self.bottom = self._pager(parent, "bottom")
        self.empty = tk.Label(
            parent,
            text="NO ITEMS IN THIS VIEW\nAdd a program or adjust the search.",
            bg=background,
            fg=SUBTEXT,
            font=("Segoe UI", 14),
            pady=80,
        )
        self._menu = None

    @staticmethod
    def _button(parent, text, command):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=CARD2,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=TEXT,
            disabledforeground=MUTED,
            highlightthickness=0,
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
            font=("Segoe UI", 8, "bold"),
        )

    def _pager(self, parent, prefix):
        frame = tk.Frame(parent, bg=self.background, name=prefix + "_pager")
        previous = self._button(frame, "← PREVIOUS", lambda: self.on_page(-1))
        previous.pack(side="left", padx=8)
        label = tk.Label(
            frame, text="", bg=self.background, fg=SUBTEXT, font=("Consolas", 9, "bold")
        )
        label.pack(side="left", expand=True, padx=8)
        following = self._button(frame, "NEXT →", lambda: self.on_page(1))
        following.pack(side="right", padx=8)
        return {"frame": frame, "previous": previous, "next": following, "label": label}

    def capture_anchor(self):
        if not self.cards:
            return None
        try:
            top = self.canvas.canvasy(0)
            visible = [
                (handle.widget.winfo_y(), key)
                for key, handle in self.cards.items()
                if handle.widget.winfo_y() + handle.widget.winfo_height() >= top
            ]
            if not visible:
                return (None, 0, top)
            y, key = min(visible)
            return (key, top - y, top)
        except tk.TclError:
            return None

    def _cancel_restore(self):
        for job in (self._restore_job, self._restore_expiry):
            if job is not None:
                try:
                    self.parent.after_cancel(job)
                except tk.TclError:
                    pass
        self._restore_job = self._restore_expiry = None
        self._pending_anchor = None

    def _apply_anchor(self):
        self._restore_job = None
        if self.closed or self._pending_anchor is None:
            return
        anchor, generation = self._pending_anchor
        if generation != self._generation:
            return
        try:
            bounds = self.canvas.bbox("all")
            if not bounds or bounds[3] <= bounds[1]:
                return
            self.canvas.configure(scrollregion=bounds)
            key, offset, old_y = anchor
            handle = self.cards.get(key)
            wanted = handle.widget.winfo_y() + offset if handle is not None else old_y
            self.canvas.yview_moveto(max(0.0, wanted / max(1, bounds[3] - bounds[1])))
        except tk.TclError:
            pass

    def _layout_changed(self, event):
        if event.widget is not self.parent or self._pending_anchor is None or self.closed:
            return
        if self._restore_job is not None:
            try:
                self.parent.after_cancel(self._restore_job)
            except tk.TclError:
                pass
        self._restore_job = self.parent.after_idle(self._apply_anchor)

    def _schedule_restore(self, anchor, generation):
        if anchor is None:
            return
        self._pending_anchor = (anchor, generation)
        self._restore_job = self.parent.after_idle(self._apply_anchor)

        def settled():
            self._restore_expiry = None
            self._apply_anchor()
            self._pending_anchor = None

        # Grid can propagate geometry through several nested frames. Keep the
        # anchor only for this short layout transaction, never for future scrolling.
        self._restore_expiry = self.parent.after(50, settled)

    def render(
        self,
        all_items,
        page_items,
        *,
        context,
        layout,
        page,
        total_pages,
        total_items,
        background=None,
    ):
        self._cancel_restore()
        anchor = self.capture_anchor() if context == self.context else None
        self._generation += 1
        generation = self._generation
        if context != self.context:
            for handle in list(self.cards.values()):
                handle.destroy()
            self.cards.clear()
            self._positions.clear()
            self.selected.clear()
            self.context = context
        self.all_items = list(all_items)
        self.page_items = list(page_items)
        valid = {id(item) for item in self.all_items}
        self.selected.intersection_update(valid)
        columns = layout["columns"]
        self.top.grid(row=0, column=0, columnspan=columns, sticky="ew", pady=(6, 4))
        if background is not None and background != self.background:
            self.background = background
            self.parent.configure(bg=background)
            self.canvas.configure(bg=background)
            for widget in (
                self.top,
                self.selection_bar,
                self.top_pager["frame"],
                self.bottom["frame"],
                self.empty,
                self.top_pager["label"],
                self.bottom["label"],
                self.selection_label,
            ):
                widget.configure(bg=background)
        expected = {id(item) for item in self.page_items}
        changed_layout = False
        for key in set(self.cards) - expected:
            self.cards.pop(key).destroy()
            self._positions.pop(key, None)
            changed_layout = True
        for index, item in enumerate(self.page_items):
            key = id(item)
            signature = self.signature(item, layout, context)
            handle = self.cards.get(key)
            if handle is None or handle.signature != signature:
                existed = handle is not None
                if existed:
                    handle.destroy()
                    self.rebuilt += 1
                else:
                    self.created += 1
                handle = self.build_card(
                    item, index, layout, key in self.selected, not existed, self
                )
                handle.signature = signature
                self.cards[key] = handle
                changed_layout = True
            position = (1 + index // columns, index % columns)
            if self._positions.get(key) != position:
                handle.widget.grid_configure(row=position[0], column=position[1])
                self._positions[key] = position
                changed_layout = True
            handle.update(item, key in self.selected)
            self.live_updates += 1
        row = 1 + (len(self.page_items) + columns - 1) // columns
        if self.page_items:
            self.empty.grid_forget()
        else:
            self.empty.grid(row=1, column=0, columnspan=columns, sticky="ew")
            row = 2
        self.bottom["frame"].grid(row=row, column=0, columnspan=columns, sticky="ew", pady=12)
        for pager in (self.top_pager, self.bottom):
            pager["previous"].configure(state="normal" if page > 0 else "disabled")
            pager["next"].configure(state="normal" if page + 1 < total_pages else "disabled")
            pager["label"].configure(
                text=f"{page + 1:02d} / {total_pages:02d}  ·  {total_items} ITEMS"
            )
        self._selection_changed(update_cards=False)
        if changed_layout:
            self._schedule_restore(anchor, generation)

    def selected_items(self):
        return [item for item in self.all_items if id(item) in self.selected]

    def toggle(self, item, *, range_select=False):
        key = id(item)
        if range_select and self._anchor_key is not None:
            order = [id(entry) for entry in self.page_items]
            if key in order and self._anchor_key in order:
                a, b = sorted((order.index(key), order.index(self._anchor_key)))
                self.selected.update(order[a : b + 1])
            else:
                self.selected.add(key)
        elif key in self.selected:
            self.selected.remove(key)
        else:
            self.selected.add(key)
        self._anchor_key = key
        self._selection_changed()

    def set_checked(self, item, selected):
        if selected:
            self.selected.add(id(item))
        else:
            self.selected.discard(id(item))
        self._anchor_key = id(item)
        self._selection_changed()

    def select_page(self):
        self.selected.update(id(item) for item in self.page_items)
        self._selection_changed()

    def clear_selection(self):
        self.selected.clear()
        self._anchor_key = None
        self._selection_changed()

    def _selection_changed(self, update_cards=True):
        count = len(self.selected_items())
        visible = {id(item) for item in self.page_items}
        hidden = len(self.selected - visible)
        self.selection_label.configure(
            text=f"{count} SELECTED" + (f" · {hidden} OFF PAGE" if hidden else ""),
            fg=NEON if count else MUTED,
        )
        for button in (self.clear_button, self.actions_button, self.delete_button):
            button.configure(state="normal" if count else "disabled")
        self.select_page_button.configure(state="normal" if self.page_items else "disabled")
        if update_cards:
            for item in self.page_items:
                handle = self.cards.get(id(item))
                if handle is not None:
                    handle.update(item, id(item) in self.selected)

    def bind_selection(self, widget, item):
        if widget.winfo_class() not in {
            "Button",
            "TButton",
            "Checkbutton",
            "TCheckbutton",
            "Entry",
            "TEntry",
        }:
            widget.bind(
                "<Control-Button-1>", lambda _event: (self.toggle(item), "break")[1], add="+"
            )
            widget.bind(
                "<Shift-Button-1>",
                lambda _event: (self.toggle(item, range_select=True), "break")[1],
                add="+",
            )
        for child in widget.winfo_children():
            self.bind_selection(child, item)

    def show_actions(self):
        if not self.selected_items():
            return
        if self._menu is None:
            self._menu = tk.Menu(
                self.parent, tearoff=False, bg=CARD2, fg=TEXT, activebackground=ACCENT
            )
        else:
            self._menu.delete(0, "end")
        for key, label in (("games", "Games"), ("apps", "Workspace"), ("founded", "Discovered")):
            self._menu.add_command(
                label="Move to " + label,
                state="disabled" if key == self.context else "normal",
                command=lambda destination=key: self.on_action("move", destination),
            )
        self._menu.add_separator()
        self._menu.add_command(label="Pin selected", command=lambda: self.on_action("pin", True))
        self._menu.add_command(label="Unpin selected", command=lambda: self.on_action("pin", False))
        try:
            self._menu.tk_popup(
                self.actions_button.winfo_rootx(),
                self.actions_button.winfo_rooty() + self.actions_button.winfo_height(),
            )
        finally:
            self._menu.grab_release()

    def dispose(self):
        self.closed = True
        self._cancel_restore()
        try:
            self.parent.unbind("<Configure>", self._configure_binding)
            for event, binding in self._wheel_bindings:
                self.canvas.unbind(event, binding)
        except tk.TclError:
            pass
        for handle in list(self.cards.values()):
            handle.destroy()
        self.cards.clear()
        self.selected.clear()
        if self._menu is not None:
            self._menu.destroy()
            self._menu = None
