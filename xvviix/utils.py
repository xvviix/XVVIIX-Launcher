"""Lightweight formatting, color and executable-path helpers."""

from datetime import datetime
from functools import lru_cache
import ntpath
import os
import random
from .constants import PALETTE


def random_color():
    return random.choice(PALETTE)


def format_time(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    elif m > 0:
        return f"{m}m"
    else:
        return f"{sec}s"


@lru_cache(maxsize=256)
def hex_to_rgb(h):
    if not h.startswith("#"):
        h = "#ffffff"
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


@lru_cache(maxsize=512)
def lerp_color(c1, c2, t):
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


def clean_path(path):
    value = os.path.expandvars(str(path or "").strip())
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


@lru_cache(maxsize=4096)
def canonical_path(path):
    value = clean_path(path)
    if not value:
        return ""
    return ntpath.normcase(ntpath.normpath(value))


def record_timestamp(epoch=None):
    moment = datetime.fromtimestamp(epoch).astimezone() if epoch else datetime.now().astimezone()
    return moment.isoformat(timespec="seconds")


def format_bytes(value):
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    precision = 0 if index == 0 else (1 if amount < 100 else 0)
    return f"{amount:.{precision}f} {units[index]}"


def ease_out_cubic(value):
    return 1 - (1 - value) ** 3


def ease_in_out_cubic(value):
    if value < 0.5:
        return 4 * value**3
    return 1 - ((-2 * value + 2) ** 3) / 2
