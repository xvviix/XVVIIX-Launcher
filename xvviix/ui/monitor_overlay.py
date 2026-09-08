"""Compact retained-canvas game monitor. Sensor polling never runs in the frame loop."""

from collections import deque
import logging
import math
import time
import tkinter as tk
from tkinter import font as tkfont

from .frame_clock import FrameClock, smooth_towards

BG = "#13171e"
TRACK = "#1e242d"
BORDER = "#29323e"
TEXT = "#e6edf5"
MUTED = "#8494aa"
CPU = "#32c6e7"
GPU = "#b472ef"
RAM = "#32d39a"
SWAP = "#42b6ed"
DISK = "#ffd151"
NET = "#f2db57"


def finite_number(value):
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def percentage(value):
    number = finite_number(value)
    return None if number is None else max(0.0, min(100.0, number))


def percent_text(value):
    number = percentage(value)
    return "--" if number is None else f"{number:.0f}%"


def rate_text(value):
    number = finite_number(value)
    if number is None:
        return "--"
    number = max(0.0, number)
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return (
                f"{number:.1f}{unit}"
                if 0 < number < 10 and unit != "B/s"
                else f"{number:.0f}{unit}"
            )
        number /= 1024
    return "--"


def memory_text(used, total, *, compact=False):
    used, total = finite_number(used), finite_number(total)
    if used is None or total is None or total <= 0:
        return "--/--"
    divisor, unit = (1024**3, "GB") if total >= 1024**3 else (1024**2, "MB")
    precision = 0 if compact and total / divisor >= 100 else 1
    return f"{max(0, used) / divisor:.{precision}f}/{total / divisor:.{precision}f} {unit}"


def uptime_text(value):
    number = finite_number(value)
    if number is None:
        return "--"
    minutes = max(0, int(number)) // 60
    days, hours = divmod(minutes // 60, 24)
    return (f"{days}d " if days else "") + f"{hours}h {minutes % 60}m"


class GameMonitorOverlay:
    """A desktop topmost overlay, not a DirectX/game-injection overlay.

    Retains all canvas objects, redraws positions at a 60 Hz target, and polls a
    lightweight immutable telemetry copy separately. Hidden/collapsed windows
    release their precision timer and do not animate.
    """

    WIDTH = 282
    HEIGHT = 566
    COMPACT_HEIGHT = 110
    DEFAULT_OPACITY = 0.80
    MIN_OPACITY = 0.55
    POLL_MS = 500
    HISTORY_SECONDS = 32.0
    HISTORY_ITEMS = 40

    def __init__(
        self,
        parent,
        snapshot_provider,
        *,
        on_close=None,
        on_visibility=None,
        on_compact=None,
        on_opacity=None,
        opacity=DEFAULT_OPACITY,
        compact=False,
        fps=60,
        scale=None,
        title="GAME MONITOR",
        clock=time.perf_counter,
        wall_clock=time.time,
        logger=None,
    ):
        self.provider = snapshot_provider
        self.on_close = on_close
        self.on_visibility = on_visibility
        self.on_compact = on_compact
        self.on_opacity = on_opacity
        self.opacity = self.DEFAULT_OPACITY
        self.clock = clock
        self.wall_clock = wall_clock
        self.LOG = logger if logger is not None else logging.getLogger("xvviix_launcher")
        self.closed = False
        self.visible = False
        self.compact = bool(compact)
        self._poll_job = None
        self._has_data = False
        self._last_timestamp = 0.0
        self._last_status = "starting"
        self._received_mono = self.clock()
        self._received_wall = self.wall_clock()
        self._history = deque(maxlen=self.HISTORY_ITEMS)
        self._targets = {key: None for key in ("cpu", "gpu", "ram", "swap")}
        self._current = {key: 0.0 for key in self._targets}
        self._texts = {}
        self._drawn_coordinates = {}
        self._item_states = {}
        self._drag = None
        self._cursor = ""
        self._history_scale = 20.0
        self.poll_count = 0
        self.snapshot_changes = 0
        if scale is None:
            dpi_scale = parent.winfo_fpixels("1i") / 96.0
            fit = max(0.75, (parent.winfo_screenheight() - 50) / self.HEIGHT)
            scale = min(1.75, max(0.85, dpi_scale), fit)
        self.scale = max(0.75, min(2.0, float(scale)))
        self.window = tk.Toplevel(parent, name="game_monitor_overlay")
        self.window.withdraw()
        self.window.title("XVVIIX — Game Monitor")
        self.window.configure(bg=BG)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.set_opacity(opacity, notify=False)
        self.window.resizable(False, False)
        self.canvas = tk.Canvas(
            self.window,
            bg=BG,
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            width=self._px(self.WIDTH),
            height=self._px(self.HEIGHT),
        )
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(root=self.window, family="Consolas", size=-self._px(11))
        self.small_font = tkfont.Font(root=self.window, family="Consolas", size=-self._px(10))
        self.title_font = tkfont.Font(
            root=self.window, family="Segoe UI", size=-self._px(14), weight="bold"
        )
        self.label_font = tkfont.Font(
            root=self.window, family="Consolas", size=-self._px(12), weight="bold"
        )
        self._build_scene(title)
        self.frame_clock = FrameClock(
            self.window.after, self.window.after_cancel, self._draw_frame, fps=fps, clock=clock
        )
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: setattr(self, "_drag", None))
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Button-3>", self._opacity_menu)
        self.window.bind("<Escape>", lambda _event: self.request_close())
        self.window.bind("<Map>", self._mapped, add="+")
        self.window.bind("<Unmap>", self._unmapped, add="+")
        self.window.bind("<Destroy>", self._destroyed, add="+")
        self.window.protocol("WM_DELETE_WINDOW", self.request_close)
        width = self._px(self.WIDTH)
        x = max(10, parent.winfo_screenwidth() - width - 25)
        self._resize(x=x, y=36)
        self._apply_mode()
        self._show_waiting("Waiting for telemetry")
        self.window.deiconify()
        self.window.lift()

    def _px(self, value):
        return max(1, int(round(value * self.scale)))

    def _line(self, *coords, **kwargs):
        return self.canvas.create_line(*(value * self.scale for value in coords), **kwargs)

    def _text(
        self, name, x, y, *, text="", fill=TEXT, anchor="w", font=None, detail=True, **kwargs
    ):
        tags = (name, "details") if detail else (name,)
        item = self.canvas.create_text(
            x * self.scale,
            y * self.scale,
            text=text,
            fill=fill,
            anchor=anchor,
            font=font or self.font,
            tags=tags,
            **kwargs,
        )
        self._texts[name] = (item, text, fill)
        return item

    def _build_scene(self, title):
        # Small vector gamepad; no emoji/font dependency or per-frame image work.
        points = (14, 20, 17, 17, 26, 17, 29, 21, 30, 28, 27, 29, 23, 25, 19, 25, 16, 29, 13, 28)
        self.canvas.create_polygon(
            *(value * self.scale for value in points),
            fill=BG,
            outline=TEXT,
            width=1.3 * self.scale,
            smooth=True,
        )
        self._line(16, 22, 22, 22, fill=CPU, width=self.scale)
        self._line(19, 19, 19, 25, fill=CPU, width=self.scale)
        self.canvas.create_oval(
            25 * self.scale,
            20 * self.scale,
            27 * self.scale,
            22 * self.scale,
            fill=TEXT,
            outline="",
        )
        self._text("title", 38, 23, text=title, font=self.title_font, detail=False)
        self._text(
            "toggle",
            231,
            22,
            text="▣",
            fill=MUTED,
            anchor="center",
            font=self.small_font,
            detail=False,
        )
        self._text(
            "close",
            257,
            22,
            text="×",
            fill=MUTED,
            anchor="center",
            font=self.title_font,
            detail=False,
        )
        self._line(9, 43, 272, 43, fill=BORDER)
        for y in (144, 230, 318, 392, 438):
            self._line(9, y, 272, y, fill=BORDER, tags="details")
        self.bars = {}
        for key, label, y, bar_y, color in (
            ("cpu", "CPU", 64, 79, CPU),
            ("gpu", "GPU", 162, 177, GPU),
            ("ram", "RAM", 247, 262, RAM),
            ("swap", "SWAP", 287, 302, SWAP),
        ):
            self._text(key + "_label", 10, y, text=label, fill=color, font=self.label_font)
            self._text(key + "_value", 271, y, text="--", anchor="e", font=self.label_font)
            self.canvas.create_rectangle(
                9 * self.scale,
                bar_y * self.scale,
                272 * self.scale,
                (bar_y + 7) * self.scale,
                fill=TRACK,
                outline="",
                tags="details",
            )
            item = self.canvas.create_rectangle(
                9 * self.scale,
                bar_y * self.scale,
                9 * self.scale,
                (bar_y + 7) * self.scale,
                fill=color,
                outline="",
                state="hidden",
                tags="details",
            )
            self.bars[key] = (item, bar_y)
        self._text(
            "cpu_detail",
            271,
            129,
            text="Clock: --  Temp: --",
            fill=MUTED,
            anchor="e",
            font=self.small_font,
        )
        self._text(
            "gpu_memory",
            271,
            197,
            text="VRAM: --  TEMP: --",
            fill=MUTED,
            anchor="e",
            font=self.small_font,
        )
        self._text(
            "gpu_detail",
            271,
            216,
            text="Clock: --  Power: --  Fan: --",
            fill=MUTED,
            anchor="e",
            font=self.small_font,
        )
        self.history_items = [
            self.canvas.create_rectangle(
                0, 0, 0, 0, fill=CPU, outline="", state="hidden", tags="details"
            )
            for _ in range(self.HISTORY_ITEMS)
        ]
        self._line(9, 117, 272, 117, fill=TRACK, tags="details")
        self._text("disk_label", 10, 335, text="DISK", fill=DISK, font=self.label_font)
        self._text("net_label", 151, 335, text="NET", fill=NET, font=self.label_font)
        self._text("disk_rates", 10, 355, text="R --  W --", font=self.small_font)
        self._text("net_rates", 151, 355, text="↓ --  ↑ --", font=self.small_font)
        self._text("disk_used", 10, 375, text="Usage: --", fill=MUTED, font=self.small_font)
        self._text("ping", 151, 375, text="Ping: --", fill=MUTED, font=self.small_font)
        self._text("uptime", 10, 409, text="Uptime: --", fill=MUTED, font=self.small_font)
        self._text(
            "process_count", 151, 409, text="Processes: --", fill=MUTED, font=self.small_font
        )
        self._text("status", 10, 426, text="WAITING", fill=MUTED, font=self.small_font)
        self._text("fps", 271, 426, text="UI -- fps", fill=MUTED, anchor="e", font=self.small_font)
        self._text("top_cpu_label", 10, 455, text="TOP 5 CPU", fill=CPU, font=self.small_font)
        self._text("top_memory_label", 151, 455, text="TOP 5 RAM", fill=RAM, font=self.small_font)
        self._line(141, 447, 141, 555, fill=BORDER, tags="details")
        for index in range(5):
            y = 477 + index * 18
            self._text(f"top_cpu_name_{index}", 10, y, text="--", font=self.small_font)
            self._text(f"top_cpu_value_{index}", 132, y, text="", anchor="e", font=self.small_font)
            self._text(f"top_memory_name_{index}", 151, y, text="--", font=self.small_font)
            self._text(
                f"top_memory_value_{index}", 271, y, text="", anchor="e", font=self.small_font
            )
        self._text(
            "compact_summary",
            12,
            65,
            text="CPU --  GPU --  RAM --",
            font=self.label_font,
            detail=False,
        )

        self._text("compact_network", 12, 89, text="NET ↓ --  ↑ --", font=self.font, detail=False)

    def _set_text(self, name, text, fill=None):
        item, old_text, old_fill = self._texts[name]
        fill = old_fill if fill is None else fill
        if text != old_text or fill != old_fill:
            self.canvas.itemconfigure(item, text=text, fill=fill)
            self._texts[name] = (item, text, fill)

    def _state(self, item, visible):
        state = "normal" if visible and not self.compact else "hidden"
        if self._item_states.get(item) != state:
            self.canvas.itemconfigure(item, state=state)
            self._item_states[item] = state

    def _coords(self, item, coords):
        scaled = tuple(round(value * self.scale, 2) for value in coords)
        if self._drawn_coordinates.get(item) != scaled:
            self.canvas.coords(item, *scaled)
            self._drawn_coordinates[item] = scaled

    def set_opacity(self, value, *, notify=True):
        number = finite_number(value)
        self.opacity = max(
            self.MIN_OPACITY, min(1.0, self.DEFAULT_OPACITY if number is None else number)
        )
        try:
            self.window.attributes("-alpha", self.opacity)
        except tk.TclError as exc:
            self.LOG.debug("Window alpha is unavailable: %s", exc)
        if notify and self.on_opacity is not None:
            self.on_opacity(self.opacity)

    def _opacity_menu(self, event):
        if self.closed:
            return
        menu = getattr(self, "_alpha_menu", None)
        if menu is None:
            menu = tk.Menu(
                self.window,
                tearoff=False,
                bg=BG,
                fg=TEXT,
                activebackground=TRACK,
                activeforeground=TEXT,
                font=self.font,
            )
            self._alpha_menu = menu
        else:
            menu.delete(0, "end")
        menu.add_command(label="Window opacity", state="disabled")
        menu.add_separator()
        for value in (0.60, 0.70, 0.80, 0.90, 1.0):
            prefix = "✓ " if abs(self.opacity - value) < 0.001 else "  "
            menu.add_command(
                label=f"{prefix}{value * 100:.0f}%",
                command=lambda selected=value: self.set_opacity(selected),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_top_rows(self, prefix, items, field):
        for index in range(5):
            item = items[index] if index < len(items) else None
            if not isinstance(item, dict):
                self._set_text(f"{prefix}_name_{index}", "--")
                self._set_text(f"{prefix}_value_{index}", "")
                continue
            name = " ".join(str(item.get("name") or "Unknown").split())[:80]
            # Use real font measurements so long names cannot cover the percentage.
            while len(name) > 2 and self.small_font.measure(name) > 84 * self.scale:
                name = name[:-2].rstrip("…") + "…"
            value = percentage(item.get(field))
            label = "--" if value is None else (f"{value:.1f}%" if value < 10 else f"{value:.0f}%")
            self._set_text(f"{prefix}_name_{index}", name)
            self._set_text(f"{prefix}_value_{index}", label)

    def _accept_snapshot(self, snapshot, now):
        cpu, gpu = snapshot.get("cpu") or {}, snapshot.get("gpu") or {}
        memory, disk = snapshot.get("memory") or {}, snapshot.get("storage") or {}
        network, system = snapshot.get("network") or {}, snapshot.get("system") or {}
        timestamp = finite_number(snapshot.get("timestamp")) or 0.0
        self._received_mono, self._received_wall = now, self.wall_clock()
        if timestamp != self._last_timestamp:
            self.snapshot_changes += 1
            self._last_timestamp = timestamp
            history = snapshot.get("cpu_history")
            if history is not None:
                self._history.clear()
                for sample in list(history)[-self.HISTORY_ITEMS :]:
                    if (
                        len(sample) == 2
                        and finite_number(sample[0]) is not None
                        and percentage(sample[1]) is not None
                    ):
                        self._history.append((float(sample[0]), percentage(sample[1])))
            elif percentage(cpu.get("percent")) is not None:
                self._history.append((timestamp, percentage(cpu.get("percent"))))
        self._history_scale = max(
            20.0, min(100.0, max((value for _ts, value in self._history), default=0.0) * 1.1)
        )
        self._targets.update(
            cpu=percentage(cpu.get("percent")),
            gpu=percentage(gpu.get("usage")),
            ram=percentage(memory.get("percent")),
            swap=percentage(memory.get("swap_percent")),
        )
        self._set_text("cpu_value", percent_text(cpu.get("percent")))
        self._set_text("gpu_value", percent_text(gpu.get("usage")))
        self._set_text("ram_value", memory_text(memory.get("used"), memory.get("total")))
        self._set_text("swap_value", memory_text(memory.get("swap_used"), memory.get("swap_total")))
        frequency, temp = (
            finite_number(cpu.get("current_mhz")),
            finite_number(cpu.get("temperature")),
        )
        freq = (
            f"{frequency / 1000:.2f} GHz"
            if frequency is not None and frequency > 0
            else "Clock: --"
        )
        self._set_text(
            "cpu_detail", freq + (f"  Temp: {temp:.0f}°C" if temp is not None else "  Temp: --")
        )
        vram = memory_text(gpu.get("vram_used"), gpu.get("vram_total"))
        temp = finite_number(gpu.get("temperature"))
        self._set_text(
            "gpu_memory", f"VRAM: {vram}  TEMP: " + (f"{temp:.0f}°C" if temp is not None else "--")
        )
        clock, power, fan = (
            finite_number(gpu.get("clock_mhz")),
            finite_number(gpu.get("power_w")),
            percentage(gpu.get("fan_percent")),
        )
        details = "Clock: " + (f"{clock:.0f}MHz" if clock is not None else "--")
        details += "  Power: " + (f"{power:.0f}W" if power is not None else "--")
        details += "  Fan: " + (f"{fan:.0f}%" if fan is not None else "--")
        self._set_text("gpu_detail", details)
        self._set_text(
            "disk_rates",
            f"R{rate_text(disk.get('read_rate'))}  W{rate_text(disk.get('write_rate'))}",
        )
        self._set_text(
            "net_rates",
            f"↓{rate_text(network.get('download_rate'))} ↑{rate_text(network.get('upload_rate'))}",
        )
        self._set_text(
            "disk_used",
            f"{percent_text(disk.get('percent'))} used ({memory_text(disk.get('used'), disk.get('total'), compact=True)})",
        )
        ping = finite_number(network.get("latency_ms"))
        self._set_text("ping", "Ping: " + (f"{ping:.0f} ms" if ping is not None else "--"))
        self._set_text("uptime", "Uptime: " + uptime_text(system.get("uptime_seconds")))
        count = finite_number(system.get("process_count"))
        self._set_text(
            "process_count", "Processes: " + (f"{max(0, count):.0f}" if count is not None else "--")
        )
        self._set_top_rows("top_cpu", list(snapshot.get("top_cpu") or [])[:5], "cpu_percent")
        self._set_top_rows(
            "top_memory", list(snapshot.get("top_memory") or [])[:5], "memory_percent"
        )
        self._set_text(
            "compact_network",
            f"NET ↓{rate_text(network.get('download_rate'))}  ↑{rate_text(network.get('upload_rate'))}",
        )
        self._set_text(
            "compact_summary",
            f"CPU {percent_text(cpu.get('percent'))}  GPU {percent_text(gpu.get('usage'))}  RAM {percent_text(memory.get('percent'))}",
        )
        self._set_text(
            "status", "DEMO · SAMPLE DATA" if snapshot.get("demo") else "LIVE TELEMETRY", RAM
        )
        self._has_data = True

    def _show_waiting(self, message):
        self._has_data = False
        for key in self._targets:
            self._targets[key] = None
            self._set_text(key + "_value", "--")
        self._set_text("status", message.upper()[:29], MUTED)
        self._set_text("compact_summary", "CPU --  GPU --  RAM --")
        self._set_text("compact_network", "NET ↓ --  ↑ --")
        self._set_top_rows("top_cpu", [], "cpu_percent")
        self._set_top_rows("top_memory", [], "memory_percent")
        for name in (
            "cpu_detail",
            "gpu_memory",
            "gpu_detail",
            "disk_rates",
            "net_rates",
            "disk_used",
            "ping",
            "uptime",
            "process_count",
        ):
            self._set_text(name, "--")
        self._history.clear()
        self._draw_frame(self.clock(), 0.1)

    def poll_now(self):
        if self.closed or not self.visible:
            return
        if self._poll_job is not None:
            self.window.after_cancel(self._poll_job)
            self._poll_job = None
        try:
            self.poll_count += 1
            snapshot = self.provider()
            if not isinstance(snapshot, dict):
                snapshot = None
            timestamp = (
                finite_number(snapshot.get("timestamp")) if isinstance(snapshot, dict) else None
            )
            online = isinstance(snapshot, dict) and snapshot.get("status") == "online"
            fresh = timestamp is not None and -2.0 <= self.wall_clock() - timestamp < 4.0
            if online and fresh:
                self._accept_snapshot(snapshot, self.clock())
                if not self.compact:
                    self.frame_clock.start()
            else:
                self.frame_clock.stop()
                reason = (
                    "Waiting for telemetry"
                    if not snapshot or snapshot.get("status") == "starting"
                    else "Telemetry unavailable"
                )
                self._show_waiting(reason)
            stats = self.frame_clock.statistics()
            self._set_text(
                "fps",
                f"UI {stats['callback_fps']:.0f} fps" if self.frame_clock.running else "UI paused",
            )
        except (
            tk.TclError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            AttributeError,
        ) as exc:
            self.LOG.debug("Overlay snapshot deferred: %s", exc)
            self.frame_clock.stop()
            if not self.closed:
                self._show_waiting("Telemetry unavailable")
        if not self.closed and self.visible:
            self._poll_job = self.window.after(self.POLL_MS, self.poll_now)

    def _draw_frame(self, now, delta):
        if self.closed or self.compact:
            return
        for key, (item, y) in self.bars.items():
            self._current[key] = smooth_towards(self._current[key], self._targets[key], delta)
            value = self._current[key]
            self._state(item, self._targets[key] is not None and value > 0.01)
            self._coords(item, (9, y, 9 + 263 * value / 100, y + 7))
        wall_now = self._received_wall + max(0, now - self._received_mono)
        left = wall_now - self.HISTORY_SECONDS
        bar_width = 263 / self.HISTORY_SECONDS - 1
        samples = list(self._history)
        for index, item in enumerate(self.history_items):
            if index >= len(samples):
                self._state(item, False)
                continue
            timestamp, value = samples[index]
            x = 9 + (timestamp - left) / self.HISTORY_SECONDS * 263
            x1, x2 = max(9, x - bar_width), min(272, x)
            self._state(item, x2 > x1 and self._has_data)
            if x2 > x1:
                self._coords(item, (x1, 117 - max(1, value / self._history_scale * 16), x2, 117))

    def _resize(self, x=None, y=None):
        if x is None:
            x, y = self.window.winfo_x(), self.window.winfo_y()
        height = self.COMPACT_HEIGHT if self.compact else self.HEIGHT
        self.canvas.configure(height=self._px(height))
        self.window.geometry(f"{self._px(self.WIDTH)}x{self._px(height)}+{int(x)}+{int(y)}")

    def _apply_mode(self):
        self.canvas.itemconfigure("details", state="hidden" if self.compact else "normal")
        self.canvas.itemconfigure("compact_summary", state="normal" if self.compact else "hidden")
        self.canvas.itemconfigure("compact_network", state="normal" if self.compact else "hidden")
        self._item_states.clear()
        if self.compact:
            self.frame_clock.stop()
        elif self.visible and self._has_data:
            self.frame_clock.start()
        self._draw_frame(self.clock(), 0.0)

    def toggle_compact(self):
        if self.closed:
            return
        self.compact = not self.compact
        self._resize()
        self._apply_mode()
        if self.on_compact is not None:
            self.on_compact(self.compact)

    def _press(self, event):
        x, y = event.x / self.scale, event.y / self.scale
        if y > 43:
            return
        if x > 244:
            self.request_close()
        elif x > 219:
            self.toggle_compact()
        else:
            self._drag = (
                event.x_root - self.window.winfo_x(),
                event.y_root - self.window.winfo_y(),
            )

    def _drag_move(self, event):
        if self._drag is not None and not self.closed:
            x, y = event.x_root - self._drag[0], event.y_root - self._drag[1]
            self.window.geometry(f"+{x}+{y}")

    def _hover(self, event):
        if self.closed:
            return
        x, y = event.x / self.scale, event.y / self.scale
        cursor = "hand2" if y < 43 and x > 219 else ("fleur" if y < 43 else "")
        if cursor != self._cursor:
            self.canvas.configure(cursor=cursor)
            self._cursor = cursor
        self._set_text("close", "×", TEXT if y < 43 and x > 244 else MUTED)
        self._set_text("toggle", "▣", TEXT if y < 43 and 219 < x <= 244 else MUTED)

    def _mapped(self, event):
        if event.widget is not self.window or self.closed or self.visible:
            return
        self.visible = True
        self.set_opacity(self.opacity, notify=False)
        self.poll_now()
        if self.on_visibility is not None:
            self.on_visibility(True)

    def _unmapped(self, event):
        if event.widget is not self.window or self.closed:
            return
        self.visible = False
        self._stop_jobs()
        if self.on_visibility is not None:
            self.on_visibility(False)

    def _stop_jobs(self):
        self.frame_clock.stop()
        if self._poll_job is not None:
            try:
                self.window.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None

    def request_close(self):
        if self.closed:
            return
        if self.on_close is not None:
            self.on_close()
        else:
            self.destroy()

    def _destroyed(self, event):
        if event.widget is self.window:
            self.closed = True
            self.visible = False
            self._stop_jobs()

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        self.visible = False
        self._stop_jobs()
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def statistics(self):
        return {
            **self.frame_clock.statistics(),
            "sensor_polls": self.poll_count,
            "snapshot_changes": self.snapshot_changes,
            "canvas_items": len(self.canvas.find_all()) if not self.closed else 0,
        }
