"""Cleanup for the Tk interpreter owned by the launcher."""

import tkinter as tk
import uuid


def cancel_pending_callbacks(window):
    """Cancel queued work before destroying the application's root window.

    Use Tcl's cancellation primitive: deleting Python commands through a
    different widget can leave the owning widget's command registry stale.
    Do not call this when closing just one dialog in a still-running app.
    """
    cancelled = 0
    try:
        jobs = window.tk.splitlist(window.tk.call("after", "info"))
        for job in jobs:
            window.tk.call("after", "cancel", job)
            cancelled += 1
    except (AttributeError, tk.TclError):
        pass
    return cancelled


class DialogVariable:
    """A Tcl-backed form value with explicit widget-owned cleanup.

    Unlike a Python Tk Variable finalizer, cleanup cannot unexpectedly run on a
    worker thread when cyclic garbage is collected after a modal dialog closes.
    """

    def __init__(self, master, value="", *, boolean=False):
        self._name = "xvviix_dialog_value_" + uuid.uuid4().hex
        self._master = master
        self._tk = master.tk
        self._boolean = boolean
        self.set(value)

        def destroyed(event):
            if event.widget is master:
                self.close()

        master.bind("<Destroy>", destroyed, add="+")

    def __str__(self):
        return self._name

    def get(self):
        if self._tk is None:
            raise RuntimeError("Dialog value is closed")
        value = self._tk.globalgetvar(self._name)
        return self._tk.getboolean(value) if self._boolean else str(value)

    def set(self, value):
        if self._tk is None:
            raise RuntimeError("Dialog value is closed")
        self._tk.globalsetvar(self._name, value)

    def trace_add(self, mode, callback):
        if self._tk is None:
            raise RuntimeError("Dialog value is closed")
        command = self._master.register(callback)
        self._tk.call("trace", "add", "variable", self._name, mode, command)
        return command

    def close(self):
        interpreter, self._tk = self._tk, None
        self._master = None
        if interpreter is not None:
            try:
                interpreter.globalunsetvar(self._name)
            except tk.TclError:
                pass
