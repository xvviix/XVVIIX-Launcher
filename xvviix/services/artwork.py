"""Bounded, lazy background artwork preparation. This module never imports Tk."""

from collections import deque
from functools import lru_cache
import logging
import threading

from ..utils import hex_to_rgb

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
except (ImportError, OSError):
    Image = ImageDraw = ImageEnhance = ImageFilter = ImageOps = None


@lru_cache(maxsize=12)
def _readability_layer(width, height, card_rgb):
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for x in range(width):
        alpha = int(238 - 132 * x / max(1, width - 1))
        draw.line((x, 0, x, height), fill=card_rgb + (alpha,))
    draw.rectangle((0, height - max(25, height // 4), width, height), fill=card_rgb + (118,))
    return layer


def compose_card_backdrop(
    source_path, width, height, accent_color, is_artwork=False, *, card_color="#0f172a"
):
    """Keep the existing card treatment, decoding large artwork at a useful size."""
    if Image is None or not source_path:
        return None
    width, height = max(220, min(720, int(width))), max(88, min(220, int(height)))
    try:
        with Image.open(source_path) as opened:
            if is_artwork:
                # JPEG decoder hints and a bounded intermediate avoid full-size RGBA buffers.
                opened.draft("RGB", (width * 3, height * 3))
                opened.thumbnail(
                    (width * 3, height * 3), Image.Resampling.LANCZOS, reducing_gap=3.0
                )
            source = opened.convert("RGBA")
        card_rgb, accent = hex_to_rgb(card_color), hex_to_rgb(accent_color)
        base = Image.new("RGBA", (width, height), card_rgb + (255,))
        if is_artwork:
            cover = ImageOps.fit(
                source, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.44)
            )
            cover = ImageEnhance.Color(cover).enhance(0.62)
            cover = ImageEnhance.Brightness(cover).enhance(0.48)
            base.alpha_composite(cover.filter(ImageFilter.GaussianBlur(0.65)))
        else:
            glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            radius, x, y = int(height * 0.95), width - int(height * 0.52), height // 2
            ImageDraw.Draw(glow).ellipse(
                (x - radius, y - radius, x + radius, y + radius), fill=accent + (74,)
            )
            base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(14, height // 4))))
            size = max(height, int(height * 1.42))
            icon = ImageOps.contain(source, (size, size), method=Image.Resampling.LANCZOS)
            icon.putalpha(icon.getchannel("A").point(lambda value: int(value * 0.25)))
            base.alpha_composite(
                icon, (width - icon.width + int(height * 0.15), (height - icon.height) // 2)
            )
        source.close()
        base.alpha_composite(_readability_layer(width, height, card_rgb))
        detail = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(detail).line(
            (int(width * 0.58), height - 1, width, height - 1), fill=accent + (96,), width=1
        )
        base.alpha_composite(detail)
        return base.convert("RGB")
    except (OSError, ValueError, TypeError, AttributeError):
        return None


class ArtworkWorker:
    """One daemon worker, deduplicated bounded requests, and explicit cancellation.

    Results are ordinary images/data, never PhotoImage objects. The caller must
    marshal the result callback to its UI thread. No thread is started on import.
    """

    def __init__(self, render, on_result, *, capacity=48, logger=None, thread_name="card-artwork"):
        self.render = render
        self.on_result = on_result
        self.capacity = max(1, int(capacity))
        self.LOG = logger if logger is not None else logging.getLogger("xvviix_launcher")
        self._condition = threading.Condition()
        self._requests = deque()
        self._pending = set()
        self._thread = None
        self.closed = False
        self.thread_name = thread_name

    def submit(self, key, *args, **kwargs):
        with self._condition:
            if self.closed:
                return False
            if key in self._pending:
                return True
            if len(self._requests) >= self.capacity:
                return False
            self._pending.add(key)
            self._requests.append((key, args, kwargs))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name=self.thread_name
                )
                self._thread.start()
            self._condition.notify()
            return True

    def cancel_pending(self):
        with self._condition:
            cancelled = [item[0] for item in self._requests]
            self._requests.clear()
            self._pending.difference_update(cancelled)
            return cancelled

    def stop(self):
        with self._condition:
            self.closed = True
            cancelled = [item[0] for item in self._requests]
            self._requests.clear()
            self._pending.difference_update(cancelled)
            self._condition.notify_all()
            return cancelled

    def _run(self):
        while True:
            with self._condition:
                while not self.closed and not self._requests:
                    self._condition.wait()
                if self.closed:
                    return
                key, args, kwargs = self._requests.popleft()
            try:
                result = self.render(*args, **kwargs)
            except Exception as exc:
                self.LOG.debug("Artwork preparation skipped: %s", exc)
                result = None
            with self._condition:
                self._pending.discard(key)
                if not self.closed:
                    try:
                        self.on_result(key, result)
                    except Exception as exc:
                        self.LOG.debug("Artwork delivery skipped: %s", exc)
                        if hasattr(result, "close"):
                            result.close()
                elif hasattr(result, "close"):
                    result.close()
