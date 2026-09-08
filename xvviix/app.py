"""Desktop composition root and remaining legacy library UI.

Backend services do not import this module. Importing the shell prepares the
existing data/log paths; use backend modules for headless tools and tests.
"""

print("STARTING...", flush=True)
_BOOTSTRAP_STARTED_AT = __import__("time").perf_counter()
import ctypes
from ctypes import wintypes
from collections import deque
import json
import logging
import ntpath
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import weakref
from . import paths
from .services import audio, artwork, launching, discovery, icons, termination, software_catalog
from .services.discovery_rules import UNNECESSARY_BASENAMES, UNNECESSARY_MARKERS
from .paths import resource_path
from .security.crypto import HAS_CRYPTOGRAPHY
from .storage.vault import VaultStore
from .constants import (
    MUSIC_ID as MUSIC_ID,
    MUSIC_TITLE as MUSIC_TITLE,
    MUSIC_ARTIST as MUSIC_ARTIST,
    MUSIC_VOLUME as MUSIC_VOLUME,
    BG as BG,
    BG2 as BG2,
    TOP_BG as TOP_BG,
    CARD as CARD,
    CARD2 as CARD2,
    TEXT as TEXT,
    SUBTEXT as SUBTEXT,
    MUTED as MUTED,
    ACCENT as ACCENT,
    ACCENT2 as ACCENT2,
    NEON as NEON,
    CYAN as CYAN,
    TAB_ACT as TAB_ACT,
    TAB_IN as TAB_IN,
    GREEN as GREEN,
    GREEN_HOVER as GREEN_HOVER,
    RED as RED,
    ORANGE as ORANGE,
    ORANGE_HOVER as ORANGE_HOVER,
    BORDER as BORDER,
    BORDER_SOFT as BORDER_SOFT,
    DWMWA_WINDOW_CORNER_PREFERENCE as DWMWA_WINDOW_CORNER_PREFERENCE,
    DWMWCP_ROUND as DWMWCP_ROUND,
    CARD_ART_CACHE_LIMIT as CARD_ART_CACHE_LIMIT,
    PAGE_SIZE as PAGE_SIZE,
    REPORT_PAGE_SIZE as REPORT_PAGE_SIZE,
    REPORT_LIMIT as REPORT_LIMIT,
    ACTIVITY_LIMIT as ACTIVITY_LIMIT,
)
from .utils import (
    random_color as random_color,
    format_time as format_time,
    hex_to_rgb as hex_to_rgb,
    rgb_to_hex as rgb_to_hex,
    lerp_color as lerp_color,
    clean_path as clean_path,
    canonical_path as canonical_path,
    record_timestamp as record_timestamp,
    format_bytes as format_bytes,
    ease_out_cubic as ease_out_cubic,
    ease_in_out_cubic as ease_in_out_cubic,
)
from . import models
from .services.hardware_monitor import (
    HardwareMonitorService as HardwareMonitorService,
    monitor_clamp_percent as monitor_clamp_percent,
    monitor_format_bytes as monitor_format_bytes,
    monitor_format_rate as monitor_format_rate,
)

try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
except ImportError as exc:
    startup_message = (
        "XVVIIX Launcher needs Python's Tk/Tcl component (tkinter).\n\n"
        "Re-run the official Python installer, choose Modify, and enable tcl/tk and IDLE.\n\n"
        f"Details: {exc}"
    )
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None, startup_message, "XVVIIX Launcher — Missing Tk", 0x10
            )
        except (AttributeError, OSError):
            print(startup_message, file=sys.stderr)
    else:
        print(startup_message, file=sys.stderr)
    raise SystemExit(1) from exc

from .ui.vault_dialogs import VaultDialogs, VaultTheme
from .ui.lifecycle import cancel_pending_callbacks, DialogVariable
from .ui.monitor_overlay import GameMonitorOverlay
from .ui.top_processes import TopProcessPanels
from .ui.library_view import LibraryView, CardHandle
from .services import autostart
from .ui.activity_view import ActivityView, activity_age
from .ui.closing import ClosingDialog
from .ui.scan_options import choose_scan_scope

# Optional integrations must never prevent the launcher from opening.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES

    HAS_DND = True
except (ImportError, OSError):
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageTk

    HAS_PIL = True
except (ImportError, OSError):
    Image = None
    ImageDraw = None
    ImageEnhance = None
    ImageFilter = None
    ImageOps = None
    ImageTk = None
    HAS_PIL = False


try:
    import psutil

    HAS_PSUTIL = True
except (ImportError, OSError):
    psutil = None
    HAS_PSUTIL = False

# NVIDIA bindings are imported only if the Monitor feature is requested.

HAS_HARDWARE_MONITOR = HAS_PSUTIL

try:
    import winsound

    HAS_WINSOUND = True
except (ImportError, OSError):
    winsound = None
    HAS_WINSOUND = False

try:
    import winreg

    HAS_WINREG = True
except (ImportError, OSError):
    winreg = None
    HAS_WINREG = False

# pygame supplies independent, volume-controlled background playback. It is
# deliberately separate from splash rendering so a failed audio device cannot
# prevent the main window from opening.


try:
    from icoextract import IconExtractor

    HAS_ICOEXTRACT = HAS_PIL
except (ImportError, OSError):
    IconExtractor = None
    HAS_ICOEXTRACT = False

try:
    import pythoncom
    import win32com.client as win32_client

    HAS_WIN32COM = True
except (ImportError, OSError):
    pythoncom = None
    win32_client = None
    HAS_WIN32COM = False

LOG = logging.getLogger("xvviix_launcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ==========================================
# APPLICATION BOOTSTRAP
# ==========================================
# The telemetry service lives in services.hardware_monitor and still feeds only
# this application's Monitor tab/overlay; it starts no independent GUI.


root = None


def report_startup_error(message):
    """Show startup failures even when launched by double-click with no console."""
    text = str(message)
    LOG.critical("Startup error: %s", text)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "XVVIIX Launcher — Startup Error", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(f"XVVIIX Launcher startup error: {text}", file=sys.stderr)


def is_admin():
    """Return the current Windows elevation state without triggering UAC."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# ==========================================
# CONSTANTS AND PATHS
# ==========================================


INSTALL_DIR = paths.install_directory()
BASE_DIR = paths.choose_data_dir(INSTALL_DIR)
ICONS_DIR = os.path.join(BASE_DIR, "icons_cache")
ASSETS_DIR = resource_path("assets")
os.makedirs(ICONS_DIR, exist_ok=True)
STARTUP_FILE_LOG_READY = False
try:
    file_handler = logging.FileHandler(
        os.path.join(BASE_DIR, "xvviix_launcher.log"), encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    LOG.addHandler(file_handler)
    STARTUP_FILE_LOG_READY = True
except OSError:
    pass

STARTUP_DIAGNOSTIC_LIMIT = 96
STARTUP_CHECKPOINT_STATUSES = frozenset(
    {
        "STARTED",
        "READY",
        "DEGRADED",
        "SKIPPED",
        "ABORTED",
        "FAILED",
        "STOPPED",
    }
)
startup_run_id = uuid.uuid4().hex[:12]
startup_diagnostics = []
_startup_checkpoint_sequence = 0
_startup_last_checkpoint_at = _BOOTSTRAP_STARTED_AT
_startup_checkpoint_lock = threading.Lock()


def startup_checkpoint(phase, status="READY", detail=""):
    """Emit one bounded, machine-parseable startup diagnostic to console and log."""
    global _startup_checkpoint_sequence, _startup_last_checkpoint_at
    phase_name = "_".join(str(phase or "UNKNOWN").upper().split())[:64]
    status_name = str(status or "READY").upper()
    if status_name not in STARTUP_CHECKPOINT_STATUSES:
        status_name = "DEGRADED"
    clean_detail = " ".join(str(detail or "").split())[:512]
    with _startup_checkpoint_lock:
        now = time.perf_counter()
        _startup_checkpoint_sequence += 1
        record = {
            "event": "startup_checkpoint",
            "run": startup_run_id,
            "sequence": _startup_checkpoint_sequence,
            "phase": phase_name,
            "status": status_name,
            "elapsed_ms": max(0, int((now - _BOOTSTRAP_STARTED_AT) * 1000)),
            "step_ms": max(0, int((now - _startup_last_checkpoint_at) * 1000)),
            "detail": clean_detail,
        }
        _startup_last_checkpoint_at = now
        startup_diagnostics.append(record)
        if len(startup_diagnostics) > STARTUP_DIAGNOSTIC_LIMIT:
            del startup_diagnostics[:-STARTUP_DIAGNOSTIC_LIMIT]
    level = (
        logging.ERROR
        if status_name in {"FAILED", "ABORTED"}
        else logging.WARNING
        if status_name == "DEGRADED"
        else logging.INFO
    )
    try:
        LOG.log(
            level,
            "STARTUP_CHECKPOINT %s",
            json.dumps(record, ensure_ascii=True, separators=(",", ":")),
        )
    except Exception as exc:
        print(f"STARTUP_CHECKPOINT {record} logging_error={exc}", file=sys.stderr, flush=True)
    return dict(record)


def startup_diagnostics_snapshot():
    """Return an isolated copy for diagnostics and startup-flow tests."""
    with _startup_checkpoint_lock:
        return [dict(record) for record in startup_diagnostics]


_import_capabilities = {
    "tk": True,
    "dnd": HAS_DND,
    "pillow": HAS_PIL,
    "cryptography": HAS_CRYPTOGRAPHY,
    "psutil": HAS_PSUTIL,
    "hardware_monitor": HAS_HARDWARE_MONITOR,
    "pygame": audio.HAS_PYGAME,
    "winsound": HAS_WINSOUND,
    "win32com": HAS_WIN32COM,
}
_missing_import_capabilities = [
    name for name, available in _import_capabilities.items() if not available
]
startup_checkpoint(
    "BOOTSTRAP_IMPORTS",
    "DEGRADED" if _missing_import_capabilities else "READY",
    "; ".join(
        f"{name}={'ready' if available else 'unavailable'}"
        for name, available in _import_capabilities.items()
    ),
)

# Seed user-data files when a packaged install directory is read-only.
if BASE_DIR != INSTALL_DIR:
    for filename in (
        "games.json",
        "apps.json",
        "founded.json",
        "reports.json",
        "activity.json",
    ):
        source = os.path.join(INSTALL_DIR, filename)
        destination = os.path.join(BASE_DIR, filename)
        if os.path.isfile(source) and not os.path.exists(destination):
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                LOG.warning("Could not seed %s: %s", filename, exc)

startup_checkpoint(
    "DATA_DIRECTORY",
    "READY" if STARTUP_FILE_LOG_READY else "DEGRADED",
    f"writable_root={BASE_DIR}; install_local={BASE_DIR == INSTALL_DIR}; icon_cache=ready; diagnostic_log={'ready' if STARTUP_FILE_LOG_READY else 'console_only'}",
)

SOUNDS = {
    "select_option": resource_path("assets/tunetank.com_menu-interface-selection.wav"),
    "hover": resource_path("assets/tunetank.com_menu-option-hover.wav"),
    "click": resource_path("assets/tunetank.com_option-hover-click.wav"),
    "cursor": resource_path("assets/tunetank.com_interface-cursor-click.wav"),
}

# Premium cinematic ambience, mastered below interface cues for long sessions.
MUSIC_FILE = resource_path("assets/xvviix_music_galactic_odyssey.ogg")
SETTINGS_FILE = os.path.join(BASE_DIR, "launcher_settings.json")

# Premium midnight-neon palette

# Windows DWM constants

# Global variables
current_scale = 1.0
active_tab = "games"
current_sort = "pinned"
tracked_processes = {}
_anims = {}
_anim_tokens = {}
_animation_serial = 0
_card_leave_jobs = {}
icon_cache = {}
card_art_cache = {}
card_art_waiters = {}
card_art_worker = None
card_art_epoch = 0
library_view = None
resize_job = None
search_job = None
page_by_tab = {"games": 0, "apps": 0, "founded": 0, "reports": 0, "monitor": 0}
drop_zone = None
drop_reset_job = None
DND_ACTIVE = False
icon_refresh_job = None
icon_update_pending = set()
header_canvas = None
header_image_ref = None
header_stats_id = None
activity_rail = None
activity_primary_lbl = None
activity_secondary_lbl = None
activity_history = None
activity_clock_job = None
_activity_rail_signature = None
music_btn = None
background_music = None
hardware_monitor = None
hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
hardware_monitor_error = ""
hardware_monitor_idle_job = None
monitor_overlay_requested = False
monitor_tab_btn = None
monitor_ui = {}
monitor_refresh_job = None
monitor_history = {
    "cpu": deque(maxlen=90),
    "gpu": deque(maxlen=90),
    "memory": deque(maxlen=90),
    "storage": deque(maxlen=90),
}
monitor_history_timestamp = 0.0
monitor_overlay = None
monitor_overlay_ui = {}
monitor_overlay_job = None
launcher_settings = {
    "music_enabled": True,
    "music_volume": MUSIC_VOLUME,
    "music_track": MUSIC_ID,
    "card_art_enabled": True,
}
startup_service = autostart.WindowsStartup()
settings_popup = None
startup_menu_value = None
startup_last_error = ""
settings_load_status = "READY"
settings_load_detail = "built-in defaults"
data_loaded = False


# Shared runtime state
ui_queue = queue.Queue()
data_lock = threading.RLock()
process_lock = threading.RLock()
monitor_stop = threading.Event()
app_exit_event = threading.Event()
launch_lock = threading.Lock()
pending_launches = set()
shell_launches = {}
launch_slots = threading.BoundedSemaphore(4)
shutdown_dialog = None
shutdown_started_at = None
background_shutdown_started = False
dirty_icon_libraries = set()
dirty_scan_libraries = set()
icon_worker = None
icon_requests = {}
icon_checked = set()
icon_save_job = None
icon_save_running = False
icon_dirty_versions = {"games": 0, "apps": 0, "founded": 0}
scan_running = False
scan_cancel = False
scan_progress_value = 0.0
scan_scope = "registered"
scan_statistics = {}
all_scanned_items = []
sys_report_running = False
sys_report_cancel = threading.Event()
sys_report_window = None
sys_report_status_lbl = None
sys_report_detail_lbl = None
sys_report_progress = None
selected_report_id = None
dirty_playtime_libraries = set()
shortcut_com_state = threading.local()


def _notify_vault_warning(message):
    if data_loaded and root is not None:
        post_ui(messagebox.showwarning, "Library recovery notice", message)


def make_vault_store(directory):
    return VaultStore(
        directory,
        lock=data_lock,
        logger=LOG,
        warning_sink=_notify_vault_warning,
        runtime_ready=lambda: data_loaded,
    )


vault = make_vault_store(BASE_DIR)


# ==========================================
# UTILITY FUNCTIONS
# ==========================================
def resolve_shortcut(path):
    if not path.lower().endswith(".lnk"):
        return path
    if not HAS_WIN32COM:
        return path
    try:
        shell = getattr(shortcut_com_state, "shell", None)
        if shell is None:
            shell = win32_client.Dispatch("WScript.Shell")
            shortcut_com_state.shell = shell
        shortcut = shell.CreateShortCut(path)
        return shortcut.Targetpath
    except Exception as exc:
        LOG.debug("Could not resolve shortcut %s: %s", path, exc)
        return path


def play_sound(sound_key):
    if not HAS_WINSOUND:
        return
    filepath = SOUNDS.get(sound_key)
    if filepath and os.path.exists(filepath):
        try:
            winsound.PlaySound(
                filepath,
                winsound.SND_FILENAME
                | winsound.SND_ASYNC
                | winsound.SND_NOWAIT
                | winsound.SND_NODEFAULT,
            )
        except (OSError, RuntimeError) as exc:
            LOG.debug("Sound playback failed: %s", exc)


def load_launcher_settings():
    """Load small user preferences without allowing a damaged file to block startup."""
    global settings_load_status, settings_load_detail
    settings_load_status = "READY"
    settings_load_detail = "built-in defaults; settings file not present"
    defaults = {
        "music_enabled": True,
        "music_volume": MUSIC_VOLUME,
        "music_track": MUSIC_ID,
        "card_art_enabled": True,
        "monitor_overlay_opacity": GameMonitorOverlay.DEFAULT_OPACITY,
        "scan_scope": "registered",
    }
    if not os.path.isfile(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ValueError("settings root must be a JSON object")
        if isinstance(stored.get("music_enabled"), bool):
            defaults["music_enabled"] = stored["music_enabled"]
        if isinstance(stored.get("card_art_enabled"), bool):
            defaults["card_art_enabled"] = stored["card_art_enabled"]
        if stored.get("scan_scope") in {"registered", "search"}:
            defaults["scan_scope"] = stored["scan_scope"]
        opacity = stored.get("monitor_overlay_opacity")
        if isinstance(opacity, (int, float)) and not isinstance(opacity, bool):
            if opacity == opacity and abs(opacity) != float("inf"):
                defaults["monitor_overlay_opacity"] = max(
                    GameMonitorOverlay.MIN_OPACITY, min(1.0, float(opacity))
                )
        # A replacement track gets its own calibrated default instead of
        # inheriting a potentially much louder volume from the previous music.
        volume = stored.get("music_volume")
        if (
            stored.get("music_track") == MUSIC_ID
            and isinstance(volume, (int, float))
            and not isinstance(volume, bool)
        ):
            defaults["music_volume"] = max(0.0, min(1.0, float(volume)))
        settings_load_detail = "validated launcher settings file"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        settings_load_status = "DEGRADED"
        settings_load_detail = f"invalid settings file; safe defaults restored: {exc}"
        LOG.warning("Could not load launcher settings: %s", exc)
    return defaults


def save_launcher_settings():
    """Atomically persist music preferences in the writable data directory."""
    temporary = f"{SETTINGS_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(launcher_settings, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SETTINGS_FILE)
    except (OSError, TypeError, ValueError) as exc:
        LOG.warning("Could not save launcher settings: %s", exc)
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def update_music_button():
    if not widget_exists(music_btn) or background_music is None:
        return
    if not background_music.available:
        music_btn._anim_idle_bg = BG2
        music_btn._anim_hover_bg = CARD2
        music_btn._anim_idle_fg = MUTED
        music_btn._anim_hover_fg = MUTED
        music_btn.configure(
            text="♫  AUDIO UNAVAILABLE",
            state="disabled",
            bg=BG2,
            fg=MUTED,
            disabledforeground=MUTED,
        )
        return

    enabled = background_music.enabled
    idle_bg = "#10243a" if enabled else BG2
    hover_bg = "#164e63" if enabled else CARD2
    foreground = NEON if enabled else MUTED
    music_btn._anim_idle_bg = idle_bg
    music_btn._anim_hover_bg = hover_bg
    music_btn._anim_idle_fg = foreground
    music_btn._anim_hover_fg = TEXT
    music_btn.configure(
        text="♫  GALACTIC  ON" if enabled else "♫  GALACTIC  OFF",
        state="normal",
        bg=idle_bg,
        fg=foreground,
        activebackground=hover_bg,
        activeforeground=TEXT,
    )


def toggle_background_music():
    if background_music is None or not background_music.available:
        return
    enabled = not background_music.enabled
    background_music.set_enabled(enabled)
    launcher_settings["music_enabled"] = enabled
    launcher_settings["music_volume"] = background_music.volume
    launcher_settings["music_track"] = MUSIC_ID
    save_launcher_settings()
    update_music_button()


def show_music_credit(_event=None):
    messagebox.showinfo(
        "XVVIIX music credit",
        f"{MUSIC_TITLE}\n{MUSIC_ARTIST}\n\nMusic by AlkaKrab\nFrom Free Sci-Fi Music Pack Vol. 2",
    )


# ==========================================
# LIBRARY STATE AND VAULT COMPOSITION
# ==========================================


games = []
apps = []
founded = []
reports = []
recent_activity = []


def load_all_data_files():
    """Load protected user data only after the vault key is available."""
    global games, apps, founded, reports, recent_activity, data_loaded
    loaded_games = vault._load(vault.GAMES_FILE)
    loaded_apps = vault._load(vault.APPS_FILE)
    loaded_founded = vault._load(vault.FOUNDED_FILE)
    loaded_reports = vault._load_auxiliary_records(
        vault.REPORTS_FILE, models.normalize_report, REPORT_LIMIT * 2
    )
    loaded_activity = vault._load_auxiliary_records(
        vault.ACTIVITY_FILE, models.normalize_activity, ACTIVITY_LIMIT
    )
    games = loaded_games
    apps = loaded_apps
    founded = loaded_founded
    reports = loaded_reports
    recent_activity = loaded_activity
    data_loaded = True
    return True


def _vault_dialogs():
    return VaultDialogs(
        vault,
        load_data=load_all_data_files,
        theme=VaultTheme(
            ACCENT=ACCENT,
            ACCENT2=ACCENT2,
            BG=BG,
            BG2=BG2,
            BORDER=BORDER,
            CARD=CARD,
            CARD2=CARD2,
            GREEN=GREEN,
            MUTED=MUTED,
            NEON=NEON,
            ORANGE=ORANGE,
            RED=RED,
            SUBTEXT=SUBTEXT,
            TEXT=TEXT,
        ),
        animate_button=bind_animated_button,
        round_corners=enable_win11_round_corners,
        logger=LOG,
    )


def show_vault_dialog(parent, setup=False):
    return _vault_dialogs().show_vault_dialog(parent, setup=setup)


def show_password_reset_dialog(parent):
    return _vault_dialogs().show_password_reset_dialog(parent)


def initialize_data_vault(parent):
    return _vault_dialogs().initialize_data_vault(parent)


def add_activity(kind, title, item=None, detail="", severity="info", epoch=None):
    """Persist one bounded activity signal and schedule the header rail update."""
    if app_exit_event.is_set():
        return None
    event_epoch = float(epoch or time.time())
    item = item if isinstance(item, dict) else {}
    event = models.normalize_activity(
        {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "timestamp": record_timestamp(event_epoch),
            "epoch": event_epoch,
            "title": title,
            "item_name": item.get("name", ""),
            "item_path": item.get("path", ""),
            "detail": detail,
            "severity": severity,
        }
    )
    with data_lock:
        if recent_activity:
            latest = recent_activity[0]
            fields = ("kind", "title", "item_path", "detail", "severity")
            if abs(event_epoch - float(latest.get("epoch", 0))) < 5 and all(
                latest.get(field) == event.get(field) for field in fields
            ):
                return latest
        recent_activity.insert(0, event)
        del recent_activity[ACTIVITY_LIMIT:]
    vault._save(vault.ACTIVITY_FILE, recent_activity)
    if root is not None:
        post_ui(update_activity_rail)
    return event


def system_report_items():
    with data_lock:
        return [entry for entry in reports if entry.get("kind") == "system_report"]


def add_report(report):
    """Add only system diagnostics; retain retired records without displaying them."""
    if not isinstance(report, dict) or report.get("kind") != "system_report":
        raise ValueError("Only system diagnostic reports can be created")
    normalized = models.normalize_report(report)
    with data_lock:
        previous = list(reports)
        active = [entry for entry in reports if entry.get("kind") == "system_report"]
        retired = [entry for entry in reports if entry.get("kind") != "system_report"][
            :REPORT_LIMIT
        ]
        reports[:] = [normalized] + active[: REPORT_LIMIT - 1] + retired
    if not vault._save(vault.REPORTS_FILE, reports):
        with data_lock:
            reports[:] = previous
        return None
    if root is not None:
        post_ui(refresh)
    return normalized


def current_list():
    return {"games": games, "apps": apps, "founded": founded}.get(active_tab, games)


def save_current(update_ui=False):
    targets = {
        "games": (vault.GAMES_FILE, games),
        "apps": (vault.APPS_FILE, apps),
        "founded": (vault.FOUNDED_FILE, founded),
    }
    filepath, data = targets.get(active_tab, targets["games"])
    saved = vault._save(filepath, data)
    if saved:
        dirty_playtime_libraries.discard(active_tab)
    if update_ui and root is not None:
        update_stats()
    return saved


def save_item_library(item):
    if any(candidate is item for candidate in games):
        return vault._save(vault.GAMES_FILE, games)
    if any(candidate is item for candidate in apps):
        return vault._save(vault.APPS_FILE, apps)
    if any(candidate is item for candidate in founded):
        return vault._save(vault.FOUNDED_FILE, founded)
    return False


def post_ui(callback, *args, **kwargs):
    if not app_exit_event.is_set():
        ui_queue.put((callback, args, kwargs))


def process_ui_queue():
    """Process worker results within a small time budget to keep Tk responsive."""
    if root is None or app_exit_event.is_set():
        return
    deadline = time.perf_counter() + 0.004
    processed = 0
    while processed < 100 and time.perf_counter() < deadline:
        try:
            callback, args, kwargs = ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            callback(*args, **kwargs)
        except (tk.TclError, RuntimeError):
            LOG.debug("Discarded a UI callback during shutdown")
        processed += 1
    delay = 16 if not ui_queue.empty() else 40
    try:
        root.after(delay, process_ui_queue)
    except tk.TclError:
        pass


# ==========================================
# SCANNER FUNCTIONS
# ==========================================


def normalize_scan_text(*values):
    text = " ".join(str(value or "") for value in values).casefold()
    return (
        " "
        + " ".join("".join(character if character.isalnum() else " " for character in text).split())
        + " "
    )


def scan_phrase_present(text, phrases):
    return any(phrase in text for phrase in phrases)


def get_executable_identity(path):
    return discovery.read_executable_identity(path)


def game_artifact_score(path):
    return min(8, len(discovery.game_evidence(path)) * 5)


def enrich_scan_item(item, source=None):
    enriched = dict(item)
    if source:
        enriched["source"] = source
    return discovery.classify_item(enriched, metadata_reader=get_executable_identity)


def classify_scan_item(item):
    return discovery.classify_item(item, metadata_reader=get_executable_identity)


def _path_in_scan_roots(path, roots):
    normalized = canonical_path(path)
    return any(
        normalized == canonical_path(root).rstrip("\\/")
        or normalized.startswith(canonical_path(root).rstrip("\\/") + "\\")
        for root in roots
    )


def extract_launch_path(raw_value, require_exists=True):
    """Extract an executable from a shortcut/DisplayIcon value."""
    value = os.path.expandvars(str(raw_value or "").strip())
    if not value:
        return ""
    if value.startswith('"'):
        closing_quote = value.find('"', 1)
        value = value[1:closing_quote] if closing_quote > 1 else value.strip('"')
    else:
        # DisplayIcon commonly ends in an icon-resource index such as ,0.
        value = value.rsplit(",", 1)[0].strip()
    value = clean_path(value)
    if ntpath.splitext(value)[1].lower() not in (".exe", ".bat"):
        return ""
    if require_exists and not os.path.isfile(value):
        return ""
    return value


def remove_duplicates(items):
    by_path = {}
    result = []
    for raw_item in items:
        path = extract_launch_path(raw_item.get("path", ""), require_exists=False)
        key = canonical_path(path)
        if not key:
            continue
        item = dict(raw_item)
        item["name"] = str(item.get("name", "")).strip() or ntpath.basename(path)
        item["path"] = path
        existing = by_path.get(key)
        if existing is None:
            by_path[key] = item
            result.append(item)
            continue
        for field in (
            "publisher",
            "product",
            "description",
            "original_filename",
            "internal_name",
            "start_menu_group",
            "install_location",
            "expected_kind",
            "old_path",
        ):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]
        sources = {value for value in (existing.get("source"), item.get("source")) if value}
        if sources:
            existing["source"] = "+".join(sorted(sources))
    return result


def identity_words(value):
    return {
        word
        for word in normalize_scan_text(value).split()
        if len(word) >= 3
        and word
        not in {
            "the",
            "and",
            "for",
            "app",
            "game",
            "launcher",
            "client",
            "edition",
            "windows",
            "program",
            "application",
        }
    }


def find_likely_executable(directory, display_name, publisher="", max_files=80):
    """Conservatively locate a renamed main EXE using immutable version metadata."""
    directory = clean_path(directory)
    if (
        os.name != "nt"
        or not discovery.is_local_scan_path(directory)
        or not os.path.isdir(directory)
    ):
        return ""
    candidates = []
    base_depth = directory.rstrip("\\/").count(os.sep)
    skipped_directories = {
        "redist",
        "redistributable",
        "installer",
        "uninstall",
        "temp",
        "tmp",
        "crashreporter",
        "crashpad",
        "plugins",
        "resources",
        "locales",
    }
    try:
        for root_dir, dirs, files in os.walk(directory):
            depth = root_dir.rstrip("\\/").count(os.sep) - base_depth
            dirs[:] = [folder for folder in dirs if folder.casefold() not in skipped_directories]
            if depth >= 4:
                dirs[:] = []
            for filename in files:
                if len(candidates) >= max_files:
                    break
                if not filename.lower().endswith(".exe"):
                    continue
                if filename.casefold() in UNNECESSARY_BASENAMES or filename.casefold().startswith(
                    "unins"
                ):
                    continue
                candidates.append(os.path.join(root_dir, filename))
            if len(candidates) >= max_files:
                break
    except OSError:
        return ""
    if not candidates:
        return ""

    wanted_words = identity_words(display_name)
    publisher_text = normalize_scan_text(publisher)

    # Rank cheaply first, then read version resources for only the strongest
    # candidates. This keeps large game folders from making scans stall.
    preliminary = []
    for candidate in candidates:
        stem = ntpath.splitext(ntpath.basename(candidate))[0]
        stem_words = identity_words(stem)
        score = 0
        if normalize_scan_text(stem) == normalize_scan_text(display_name):
            score += 10
        if wanted_words:
            score += int(5 * len(wanted_words & stem_words) / len(wanted_words))
        if ntpath.dirname(candidate).casefold() == directory.casefold():
            score += 2
        try:
            size = os.path.getsize(candidate)
            if size >= 20 * 1024 * 1024:
                score += 2
            elif size >= 2 * 1024 * 1024:
                score += 1
        except OSError:
            pass
        preliminary.append((score, candidate))
    preliminary.sort(key=lambda entry: entry[0], reverse=True)

    ranked = []
    for preliminary_score, candidate in preliminary[:18]:
        stem = ntpath.splitext(ntpath.basename(candidate))[0]
        stem_words = identity_words(stem)
        metadata = get_executable_identity(candidate)
        product = metadata.get("product") or metadata.get("description") or ""
        product_words = identity_words(product)
        score = preliminary_score
        if product and normalize_scan_text(product) == normalize_scan_text(display_name):
            score += 12
        if wanted_words:
            score += int(7 * len(wanted_words & product_words) / len(wanted_words))
        company = normalize_scan_text(metadata.get("publisher"))
        if publisher_text.strip() and company == publisher_text:
            score += 4
        candidate_text = normalize_scan_text(stem, product)
        if scan_phrase_present(candidate_text, UNNECESSARY_MARKERS):
            score -= 12
        ranked.append((score, candidate))

    ranked.sort(key=lambda entry: entry[0], reverse=True)
    best_score, best_path = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else -99
    if best_score >= 7 and best_score >= second_score + 2:
        return best_path
    if len(ranked) == 1 and best_score >= 10:
        return best_path
    return ""


def scan_broken_library_paths(allowed_roots=None):
    """Look beside missing library paths for EXEs renamed after being added."""
    if os.name != "nt":
        return []
    with data_lock:
        snapshots = [
            ("game", item.get("name", ""), item.get("path", ""), dict(item.get("identity", {})))
            for item in games
        ] + [
            ("app", item.get("name", ""), item.get("path", ""), dict(item.get("identity", {})))
            for item in apps
        ]
    recovered_candidates = []
    for expected_kind, name, old_path, identity in snapshots:
        if scan_cancel:
            break
        old_path = clean_path(old_path)
        if allowed_roots and not _path_in_scan_roots(old_path, allowed_roots):
            continue
        if not old_path or os.path.isfile(old_path):
            continue
        parent = ntpath.dirname(old_path)
        candidate = find_likely_executable(parent, name, identity.get("publisher", ""))
        if not candidate:
            continue
        recovered_candidates.append(
            {
                "name": name,
                "path": candidate,
                "publisher": identity.get("publisher", ""),
                "product": identity.get("product", name),
                "source": "recovery",
                "expected_kind": expected_kind,
                "old_path": old_path,
            }
        )
    return recovered_candidates


def scan_start_menu():
    found = []
    paths = [
        os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            r"Microsoft\Windows\Start Menu\Programs",
        ),
        os.path.join(
            os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming")),
            r"Microsoft\Windows\Start Menu\Programs",
        ),
    ]
    for base in paths:
        if not os.path.isdir(base):
            continue
        for root_dir, _dirs, files in os.walk(base):
            if scan_cancel:
                return found
            for filename in files:
                if not filename.lower().endswith(".lnk"):
                    continue
                shortcut_path = os.path.join(root_dir, filename)
                raw_target = resolve_shortcut(shortcut_path)
                if not discovery.is_local_scan_path(raw_target):
                    continue
                target = extract_launch_path(raw_target)
                if target:
                    relative_group = os.path.relpath(root_dir, base)
                    start_menu_group = (
                        "" if relative_group == "." else relative_group.split(os.sep, 1)[0]
                    )
                    found.append(
                        {
                            "name": os.path.splitext(filename)[0],
                            "path": target,
                            "source": "start_menu",
                            "start_menu_group": start_menu_group,
                        }
                    )
    return found


def _registry_value(key, name, default=""):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def scan_registry(allowed_roots=None, include_locations=False):
    if not HAS_WINREG:
        return []
    found = []
    key_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    for hive in hives:
        for key_path in key_paths:
            if scan_cancel:
                return found
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        if scan_cancel:
                            return found
                        try:
                            subkey_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                name = str(_registry_value(subkey, "DisplayName", "")).strip()
                                system_component = str(
                                    _registry_value(subkey, "SystemComponent", 0) or 0
                                ).strip()
                                if (
                                    not name
                                    or system_component == "1"
                                    or software_catalog.is_runtime_component(name)
                                ):
                                    continue
                                publisher = str(_registry_value(subkey, "Publisher", "")).strip()
                                install_location = clean_path(
                                    str(_registry_value(subkey, "InstallLocation", "")).strip()
                                )
                                if (
                                    allowed_roots
                                    and install_location
                                    and not _path_in_scan_roots(install_location, allowed_roots)
                                ):
                                    continue
                                icon_target = extract_launch_path(
                                    _registry_value(subkey, "DisplayIcon", ""), require_exists=False
                                )
                                target = (
                                    icon_target
                                    if discovery.is_local_scan_path(icon_target)
                                    and os.path.isfile(icon_target)
                                    else ""
                                )
                                target = software_catalog.preferred_registered_executable(
                                    name, target, install_location
                                )
                                if target and (
                                    discovery.helper_executable(target)
                                    or software_catalog.is_runtime_component(name, target)
                                ):
                                    target = ""
                                if not target and install_location:
                                    target = find_likely_executable(
                                        install_location, name, publisher
                                    )
                                if (
                                    name
                                    and (target or (include_locations and install_location))
                                    and (
                                        not allowed_roots
                                        or _path_in_scan_roots(target, allowed_roots)
                                    )
                                ):
                                    found.append(
                                        {
                                            "name": name,
                                            "registered_name": name,
                                            "path": target,
                                            "publisher": publisher,
                                            "product": name,
                                            "install_location": install_location,
                                            "source": "registry",
                                        }
                                    )
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def scan_window_exists():
    try:
        return scan_window is not None and bool(scan_window.winfo_exists())
    except (NameError, tk.TclError):
        return False


def request_scan_cancel(close_window=False):
    global scan_cancel
    scan_cancel = True
    if scan_window_exists():
        scan_status_lbl.configure(text="CANCELLING...", fg=ORANGE)
        if close_window:
            scan_window.grab_release()
            scan_window.withdraw()


def create_scan_window():
    global scan_window, scan_count_lbl, scan_name_lbl, scan_progress, preview_box, scan_status_lbl
    global scan_breakdown_lbl, scan_progress_value
    scan_window = tk.Toplevel(root)
    scan_window.title("System Scanner")
    scan_window.geometry("820x590")
    scan_window.minsize(700, 510)
    scan_window.configure(bg=BG)
    scan_window.transient(root)
    scan_window.grab_set()
    scan_window.protocol("WM_DELETE_WINDOW", lambda: request_scan_cancel(True))

    tk.Frame(scan_window, bg=ACCENT, height=4).pack(fill="x")
    tk.Label(
        scan_window,
        text="SYSTEM DISCOVERY",
        bg=BG,
        fg=TEXT,
        font=("Segoe UI Black", 25, "bold"),
    ).pack(pady=(20, 2))
    tk.Label(
        scan_window,
        text="LOCAL EVIDENCE  //  GAMES · APPS · MANUAL REVIEW",
        bg=BG,
        fg=NEON,
        font=("Segoe UI", 9, "bold"),
    ).pack(pady=(0, 10))
    scan_status_lbl = tk.Label(
        scan_window,
        text="PREPARING...",
        bg=BG,
        fg=GREEN,
        font=("Segoe UI", 14, "bold"),
    )
    scan_status_lbl.pack(pady=5)
    scan_count_lbl = tk.Label(
        scan_window,
        text="FOUND: 0",
        bg=BG,
        fg=TEXT,
        font=("Segoe UI", 20, "bold"),
    )
    scan_count_lbl.pack(pady=(8, 2))
    scan_breakdown_lbl = tk.Label(
        scan_window,
        text="GAMES 0   ·   APPS 0   ·   REVIEW 0   ·   FILTERED 0   ·   RECOVERED 0",
        bg=BG,
        fg=SUBTEXT,
        font=("Consolas", 9, "bold"),
    )
    scan_breakdown_lbl.pack(pady=(0, 6))

    scan_progress = tk.Canvas(
        scan_window,
        width=600,
        height=30,
        bg=CARD2,
        highlightthickness=2,
        highlightbackground=ACCENT,
    )
    scan_progress.pack(pady=12)
    scan_progress.create_rectangle(0, 0, 600, 30, fill=CARD2, outline="", tags="progress-bg")
    scan_progress.create_rectangle(0, 0, 0, 30, fill=ACCENT, outline="", tags="progress-fill")
    scan_progress.create_text(
        300, 15, text="0%", fill=TEXT, font=("Segoe UI", 11, "bold"), tags="progress-text"
    )
    scan_progress_value = 0.0
    scan_name_lbl = tk.Label(
        scan_window,
        text="Starting scan...",
        bg=BG,
        fg=SUBTEXT,
        font=("Consolas", 11),
    )
    scan_name_lbl.pack(pady=6)

    preview_frame = tk.Frame(scan_window, bg=BG)
    preview_frame.pack(fill="both", expand=True, padx=30, pady=10)
    tk.Label(
        preview_frame,
        text="RECENT SIGNALS",
        bg=BG,
        fg=NEON,
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 5))
    preview_box = tk.Listbox(
        preview_frame,
        bg=CARD,
        fg=TEXT,
        font=("Consolas", 10),
        selectbackground=ACCENT,
        selectforeground=TEXT,
        highlightbackground=ACCENT,
        highlightthickness=1,
    )
    preview_box.pack(fill="both", expand=True)
    cancel_button = tk.Button(
        scan_window,
        text="CANCEL",
        command=request_scan_cancel,
        bg=RED,
        fg=TEXT,
        activebackground="#dc2626",
        activeforeground=TEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=24,
        pady=8,
    )
    bind_animated_button(cancel_button, RED, "#f87171", TEXT, TEXT)
    cancel_button.pack(pady=(4, 16))
    fade_window(scan_window, 0.0, 1.0, 220)


def set_scan_phase(text):
    if scan_window_exists():
        scan_status_lbl.configure(text=text)
        animate_widget_color(scan_status_lbl, "fg", GREEN, 180, 9, GREEN)
        scan_name_lbl.configure(text=text.title())


def animate_scan_progress(target_percent):
    if not scan_window_exists():
        return
    start = scan_progress_value
    target = max(0.0, min(100.0, float(target_percent)))

    def update(progress):
        global scan_progress_value
        scan_progress_value = start + (target - start) * progress
        scan_progress.coords("progress-fill", 0, 0, 6 * scan_progress_value, 30)
        scan_progress.itemconfigure("progress-text", text=f"{int(round(scan_progress_value))}%")

    start_animation(scan_progress, "scan-progress", 190, 11, update, easing=ease_in_out_cubic)


def update_scan_progress(current, total, item_name="", kind="", breakdown=None):
    if not scan_window_exists():
        return
    percent = 100 if total == 0 else min(100, int(current * 100 / total))
    animate_scan_progress(percent)
    scan_count_lbl.configure(text=f"ANALYZED: {current} / {total}")
    scan_name_lbl.configure(text=item_name or f"Processed {current} of {total}")
    if breakdown:
        filtered = sum(breakdown.get(value, 0) for value in ("driver", "system", "ignore"))
        scan_breakdown_lbl.configure(
            text=(
                f"GAMES {breakdown.get('game', 0)}   ·   "
                f"APPS {breakdown.get('app', 0)}   ·   "
                f"REVIEW {breakdown.get('unknown', 0)}   ·   "
                f"FILTERED {filtered}   ·   "
                f"RECOVERED {breakdown.get('recovery', 0)}"
            )
        )
    if item_name:
        labels = {
            "game": ("GAME", GREEN),
            "app": ("APP", CYAN),
            "unknown": ("REVIEW", ORANGE),
            "driver": ("DRIVER · FILTERED", MUTED),
            "system": ("SYSTEM · FILTERED", MUTED),
            "ignore": ("IRRELEVANT · FILTERED", MUTED),
        }
        label, foreground = labels.get(kind, ("FOUND", TEXT))
        preview_box.insert(0, f"{label:<20} {item_name}")
        try:
            preview_box.itemconfig(0, fg=foreground)
        except tk.TclError:
            pass
        if preview_box.size() > 15:
            preview_box.delete(15, tk.END)


def scanned_library_item(scanned):
    identity = models.normalize_identity(scanned.get("identity"))
    return {
        "name": str(scanned.get("name", "")).strip() or ntpath.basename(scanned.get("path", "")),
        "path": clean_path(scanned.get("path", "")),
        "trainer": "",
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color(),
        "identity": identity,
    }


def apply_intelligent_scan_results(items, existing_classifications=None):
    """Apply read-only scan findings on the worker; preserve user-curated libraries.

    No filesystem/PE queries run under model locks. Uncertain repair candidates
    stay in Discovered rather than overwriting an unrelated path automatically.
    """
    del existing_classifications
    if scan_cancel or app_exit_event.is_set():
        raise discovery.ScanCancelled()
    summary = {"game": 0, "app": 0, "unknown": 0, "filtered": 0, "recovered": 0, "moved": 0}
    changed = set()
    with data_lock:
        if scan_cancel or app_exit_event.is_set():
            raise discovery.ScanCancelled()
        path_sets = {
            "games": {canonical_path(item.get("path", "")) for item in games},
            "apps": {canonical_path(item.get("path", "")) for item in apps},
            "founded": {canonical_path(item.get("path", "")) for item in founded},
        }
        known = set().union(*path_sets.values())
        for scanned in items:
            kind = scanned.get("kind", "unknown")
            if kind in ("ignore", "driver", "system"):
                summary["filtered"] += 1
                continue
            key = canonical_path(scanned.get("path", ""))
            if not key or key in known:
                continue
            # A renamed-path suggestion is deliberately reviewed unless immutable
            # identity metadata clearly matches the exact missing library entry.
            existing = None
            if scanned.get("old_path"):
                old_key = canonical_path(scanned["old_path"])
                for library_name, library in (("games", games), ("apps", apps)):
                    existing = next(
                        (
                            item
                            for item in library
                            if canonical_path(item.get("path", "")) == old_key
                        ),
                        None,
                    )
                    if existing is not None:
                        old_id, new_id = existing.get("identity", {}), scanned.get("identity", {})
                        matches = sum(
                            bool(old_id.get(field))
                            and normalize_scan_text(old_id[field])
                            == normalize_scan_text(new_id.get(field))
                            for field in ("publisher", "product", "original_filename")
                        )
                        if matches >= 2:
                            existing["path"] = scanned["path"]
                            existing["identity"] = models.normalize_identity(new_id)
                            changed.add(library_name)
                            known.add(key)
                            summary["recovered"] += 1
                        else:
                            existing = None
                            kind = "unknown"
                        break
                if existing is not None:
                    continue
            confidence = float(scanned.get("confidence", 0) or 0)
            if kind not in ("game", "app") or confidence < 0.88:
                kind = "unknown"
            target_name = {"game": "games", "app": "apps", "unknown": "founded"}[kind]
            target = {"games": games, "apps": apps, "founded": founded}[target_name]
            target.append(scanned_library_item(scanned))
            known.add(key)
            changed.add(target_name)
            summary[kind] += 1
        dirty_scan_libraries.update(changed)
    failures = []
    for name, filepath, library in (
        ("games", vault.GAMES_FILE, games),
        ("apps", vault.APPS_FILE, apps),
        ("founded", vault.FOUNDED_FILE, founded),
    ):
        if name in changed:
            if vault._save(filepath, library):
                dirty_scan_libraries.discard(name)
            else:
                failures.append(name)
    if failures:
        raise OSError(
            "Could not save scan results for: "
            + ", ".join(failures)
            + ". Existing encrypted files were retained; retry saving before exit."
        )
    return summary


def set_scan_button_running(running):
    scan_btn.configure(state="disabled" if running else "normal")
    target = lerp_color(GREEN, CARD2, 0.65) if running else GREEN
    animate_widget_color(scan_btn, "bg", target, 200, 10, GREEN)
    animate_widget_color(scan_btn, "fg", SUBTEXT if running else TEXT, 200, 10, TEXT)


def finish_scan(summary):
    global scan_running
    if app_exit_event.is_set():
        return
    scan_running = False
    icon_checked.clear()
    set_scan_button_running(False)
    refresh()
    if scan_window_exists():
        partial = bool(scan_statistics.get("limit_reached"))
        scan_status_lbl.configure(
            text="PARTIAL SCAN — SAFETY LIMIT" if partial else "SCAN COMPLETE",
            fg=ORANGE if partial else GREEN,
        )
        scan_count_lbl.configure(
            text=f"ADDED: {summary['game']} GAMES · {summary['app']} APPS · {summary['unknown']} FOR REVIEW"
        )
        detail = f"Filtered {summary['filtered']} helper/system files; repaired {summary['recovered']} strongly matched paths."
        if scan_scope == "search":
            detail += f" Folders: {scan_statistics.get('directories', 0)}; inaccessible: {scan_statistics.get('inaccessible', 0)}; links/offline: {scan_statistics.get('skipped_links', 0) + scan_statistics.get('skipped_offline', 0)}."
        scan_name_lbl.configure(text=detail, wraplength=720)
        animate_scan_progress(100)
        root.after(1500, lambda: close_scan_window(open_founded=True))


def close_scan_window(open_founded=False):
    global scan_window
    if not scan_window_exists():
        scan_window = None
        if open_founded:
            switch_tab("founded")
        return
    window = scan_window

    def finalize():
        global scan_window
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            pass
        if scan_window is window:
            scan_window = None
        if open_founded:
            switch_tab("founded")

    fade_window(window, 1.0, 0.0, 160, finalize)


def cancel_scan_ui():
    global scan_running
    scan_running = False
    set_scan_button_running(False)
    if scan_window_exists():
        scan_status_lbl.configure(text="SCAN CANCELLED", fg=ORANGE)
        root.after(500, close_scan_window)


def fail_scan_ui(message):
    global scan_running
    scan_running = False
    set_scan_button_running(False)
    if scan_window_exists():
        scan_status_lbl.configure(text="SCAN FAILED", fg=RED)
        scan_name_lbl.configure(text=message)
    messagebox.showerror("Scanner Error", message)


def _scan_cancelled():
    return scan_cancel or app_exit_event.is_set()


def update_drive_scan_progress(stats, path):
    if not scan_window_exists():
        return
    scan_count_lbl.configure(
        text=f"FOLDERS {stats['directories']}  ·  LAUNCHERS {stats['executables']}"
    )
    scan_name_lbl.configure(text=str(path)[-115:], wraplength=720)
    scan_progress.itemconfigure(
        "progress-text", text=f"LOCATIONS {stats['roots_done']} / {stats['roots_total']}"
    )
    scan_breakdown_lbl.configure(
        text=f"SKIPPED LINKS {stats['skipped_links']}  ·  OFFLINE {stats['skipped_offline']}  ·  NO ACCESS {stats['inaccessible']}"
    )


def scan_all_programs(scope="registered"):
    global scan_statistics
    com_initialized = False
    try:
        if scope not in discovery.scan_modes():
            raise ValueError("Only Control Panel or usual installation locations can be scanned")
        if HAS_WIN32COM:
            pythoncom.CoInitialize()
            com_initialized = True
        discovery.reset_caches()
        post_ui(set_scan_phase, "READING CONTROL PANEL APPLICATIONS...")
        registry_items = scan_registry(include_locations=scope == "search")
        if _scan_cancelled():
            raise discovery.ScanCancelled()
        raw_items = [item for item in registry_items if item.get("path")]
        scan_statistics = {}
        if scope == "search":
            post_ui(set_scan_phase, "SEARCHING USUAL INSTALLATION LOCATIONS...")
            roots = software_catalog.known_installation_roots(registry_items)
            roots = [path for path in roots if discovery.is_local_scan_path(path)]
            stats = discovery.ScanStats()
            for item in discovery.iter_executables(
                roots,
                cancelled=_scan_cancelled,
                stats=stats,
                progress=lambda state, path: post_ui(update_drive_scan_progress, state, path),
            ):
                search_root = item.get("scan_root", "")
                root_name = ntpath.basename(search_root.rstrip("\\/")).casefold()
                if root_name not in {"program files", "program files (x86)", "programs"}:
                    item["install_root"] = search_root
                raw_items.append(item)
            raw_items.extend(scan_start_menu())
            scan_statistics = discovery.asdict(stats)
        if _scan_cancelled():
            raise discovery.ScanCancelled()
        raw_items = remove_duplicates(raw_items)
        classified = []
        breakdown = {kind: 0 for kind in ("game", "app", "unknown", "driver", "system", "ignore")}
        breakdown["recovery"] = 0
        last_notice = 0.0
        post_ui(set_scan_phase, "SELECTING REAL APPLICATIONS...")
        for index, raw in enumerate(raw_items, 1):
            if _scan_cancelled():
                raise discovery.ScanCancelled()
            item = classify_scan_item(raw)
            classified.append(item)
            kind = item.get("kind", "unknown")
            breakdown[kind] = breakdown.get(kind, 0) + 1
            now = time.monotonic()
            if now - last_notice >= 0.10 or index == len(raw_items):
                post_ui(
                    update_scan_progress,
                    index,
                    len(raw_items),
                    item.get("name", ""),
                    kind,
                    dict(breakdown),
                )
                last_notice = now
        classified = software_catalog.deduplicate_applications(classified)
        if _scan_cancelled():
            raise discovery.ScanCancelled()
        post_ui(set_scan_phase, "SAVING DEDUPLICATED RESULTS...")
        summary = apply_intelligent_scan_results(classified)
        add_activity(
            "scan",
            "Application scan completed",
            detail=f"{summary['game']} games, {summary['app']} apps, {summary['unknown']} for review; {scope} mode",
            severity="success",
        )
        post_ui(finish_scan, summary)
    except discovery.ScanCancelled:
        post_ui(cancel_scan_ui)
    except Exception as exc:
        LOG.exception("Local application scan failed")
        post_ui(fail_scan_ui, str(exc))
    finally:
        if hasattr(shortcut_com_state, "shell"):
            del shortcut_com_state.shell
        if com_initialized:
            pythoncom.CoUninitialize()


def start_auto_scan(scope=None):
    global scan_cancel, scan_running, scan_scope
    if app_exit_event.is_set():
        return
    if scan_running:
        if scan_window_exists():
            scan_window.deiconify()
            scan_window.lift()
        return
    if scope is None:
        scope = choose_scan_scope(root, launcher_settings.get("scan_scope", "registered"))
    if scope not in ("registered", "search") or app_exit_event.is_set():
        return
    scan_scope = scope
    launcher_settings["scan_scope"] = scope
    save_launcher_settings()
    scan_cancel = False
    scan_running = True
    create_scan_window()
    set_scan_button_running(True)
    threading.Thread(
        target=scan_all_programs, args=(scope,), daemon=True, name="system-scanner"
    ).start()


# ==========================================
# ITEM MANAGEMENT
# ==========================================
def library_has_path(library, path, exclude=None):
    key = canonical_path(path)
    return any(
        candidate is not exclude and canonical_path(candidate.get("path", "")) == key
        for candidate in library
    )


def _library_targets():
    return {
        "games": (games, vault.GAMES_FILE, "Games"),
        "apps": (apps, vault.APPS_FILE, "Workspace"),
        "founded": (founded, vault.FOUNDED_FILE, "Discovered"),
    }


def _perform_library_action(items, source_name, action, destination=None):
    targets = _library_targets()
    if source_name not in targets or action not in {"delete", "move", "pin"}:
        return {"changed": 0, "skipped": 0}
    source, source_file, source_label = targets[source_name]
    items = list(
        {
            id(item): item for item in items if any(candidate is item for candidate in source)
        }.values()
    )
    if not items:
        return {"changed": 0, "skipped": 0}
    if action == "move" and (destination not in targets or destination == source_name):
        return {"changed": 0, "skipped": len(items)}
    if action == "delete":
        names = "\n".join(str(item.get("name", "Application")) for item in items[:5])
        if len(items) > 5:
            names += f"\n…and {len(items) - 5} more selected entries"
        if not messagebox.askyesno(
            "Remove library entries?",
            f"Remove {len(items)} selected entry/entries from {source_label}?\n\n{names}\n\nThis removes launcher entries only. Program files are not deleted or uninstalled, and running programs stay open.",
            icon="warning",
        ):
            return {"changed": 0, "skipped": 0}
    order = [destination, source_name] if action == "move" else [source_name]
    changed, skipped, failure, rollback_failed = 0, 0, "", False
    with data_lock:
        previous_lists = {name: list(targets[name][0]) for name in order}
        previous_pins = [(item, item.get("pinned", False)) for item in items]
        disk_before = {}
        try:
            for name in order:
                path = targets[name][1]
                for file in (path, path + ".bak"):
                    if os.path.isfile(file):
                        with open(file, "rb") as handle:
                            disk_before[file] = handle.read()
                    else:
                        disk_before[file] = None
            if action == "move":
                target = targets[destination][0]
                known = {canonical_path(item.get("path", "")) for item in target}
                for item in items:
                    key = canonical_path(item.get("path", ""))
                    if key in known:
                        skipped += 1
                        continue
                    source[:] = [candidate for candidate in source if candidate is not item]
                    target.append(item)
                    known.add(key)
                    changed += 1
            elif action == "delete":
                selected = {id(item) for item in items}
                source[:] = [item for item in source if id(item) not in selected]
                changed = len(items)
            else:
                for item in items:
                    if bool(item.get("pinned")) != bool(destination):
                        item["pinned"] = bool(destination)
                        changed += 1
            if changed:
                # Destination first: interruption cannot erase the only saved copy.
                for name in order:
                    if not vault._save(targets[name][1], targets[name][0]):
                        raise OSError(f"Could not save {targets[name][2]}")
        except (OSError, ValueError, TypeError) as exc:
            failure = str(exc)
            for name, previous in previous_lists.items():
                targets[name][0][:] = previous
            for item, previous in previous_pins:
                item["pinned"] = previous
            for file, payload in disk_before.items():
                try:
                    if payload is None:
                        if os.path.exists(file):
                            os.remove(file)
                    else:
                        vault._atomic_write_bytes(file, payload)
                except OSError:
                    rollback_failed = True
            changed = 0
    if failure:
        messagebox.showerror(
            "Library change was not saved",
            failure
            + (
                "\nKeep your backups; some files could not be restored."
                if rollback_failed
                else "\nPrevious library data was restored."
            ),
        )
    elif changed:
        label = {
            "delete": "Removed library entries",
            "move": "Moved library entries",
            "pin": "Updated pinned entries",
        }[action]
        detail = f"{changed} from {source_label}"
        if action == "move":
            detail += f" to {targets[destination][2]}"
        if skipped:
            detail += f"; {skipped} duplicates skipped"
        add_activity("library", label, detail=detail, severity="success")
    if root is not None and widget_exists(root):
        refresh()
    if skipped and not changed:
        messagebox.showinfo(
            "Already present",
            f"The {skipped} selected entry/entries already exist in the destination. Nothing was removed.",
        )
    return {"changed": changed, "skipped": skipped, "failed": bool(failure)}


def bulk_library_action(action, destination=None):
    if library_view is None or app_exit_event.is_set():
        return
    return _perform_library_action(library_view.selected_items(), active_tab, action, destination)


def move_to_games(item):
    return _perform_library_action([item], "founded", "move", "games")


def move_to_apps(item):
    return _perform_library_action([item], "founded", "move", "apps")


def sort_items(lst):
    if current_sort == "name":
        return sorted(lst, key=lambda g: g["name"].lower())
    elif current_sort == "playtime":
        return sorted(lst, key=lambda g: g["playtime"], reverse=True)
    else:
        return sorted(lst, key=lambda g: (not g["pinned"], g["name"].lower()))


def toggle_pin(item):
    return _perform_library_action([item], active_tab, "pin", not item.get("pinned", False))


def delete_item(item):
    return _perform_library_action([item], active_tab, "delete")


def move_item(item):
    return _perform_library_action(
        [item], active_tab, "move", "apps" if active_tab == "games" else "games"
    )


def rename_item(item):
    new_name = simpledialog.askstring("Rename", "Enter new name:", initialvalue=item["name"])
    if new_name and new_name.strip():
        previous_name = item["name"]
        with data_lock:
            item["name"] = new_name.strip()
        if save_current():
            add_activity(
                "library",
                "Renamed library entry",
                item,
                detail=f"{previous_name} → {item['name']}",
                severity="success",
            )
        else:
            with data_lock:
                item["name"] = previous_name
        refresh()


def open_file_location(item):
    path = item.get("path", "")
    if os.path.exists(path):
        os.startfile(os.path.dirname(os.path.abspath(path)))
    else:
        messagebox.showerror("Error", "File not found")


def find_path_conflict(path, exclude=None):
    for label, library in (("Games", games), ("Workspace", apps), ("Discovered", founded)):
        if library_has_path(library, path, exclude=exclude):
            return label
    return ""


def refresh_item_identity_in_background(item, path):
    """Refresh stable executable identity after a manual path repair."""

    def worker():
        metadata = get_executable_identity(path)
        if not metadata:
            return
        with data_lock:
            still_present = any(candidate is item for candidate in games + apps + founded)
            if not still_present or canonical_path(item.get("path", "")) != canonical_path(path):
                return
            old_identity = dict(item.get("identity", {}))
            updated_identity = dict(old_identity)
            updated_identity.update(metadata)
            updated_identity["source"] = "manual_location"
            item["identity"] = models.normalize_identity(updated_identity)
        if not save_item_library(item):
            with data_lock:
                if old_identity:
                    item["identity"] = old_identity
                else:
                    item.pop("identity", None)

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"identity-refresh-{uuid.uuid4().hex[:8]}",
    ).start()


def change_item_location(item):
    """Repair one launch path while preserving every other user field."""
    if is_item_running(item):
        messagebox.showwarning(
            "Change Location",
            f"End {item.get('name', 'this task')} before changing its launch location.",
        )
        return
    old_path = clean_path(item.get("path", ""))
    selected = filedialog.askopenfilename(
        title=f"Change Location — {item.get('name', 'Launcher')}",
        initialdir=os.path.dirname(old_path) if old_path else BASE_DIR,
        filetypes=[
            ("Launchable files", "*.exe *.bat"),
            ("Executable files", "*.exe"),
            ("Batch files", "*.bat"),
            ("All files", "*.*"),
        ],
    )
    new_path = clean_path(selected)
    if not new_path or canonical_path(new_path) == canonical_path(old_path):
        return
    if ntpath.splitext(new_path)[1].lower() not in (".exe", ".bat"):
        messagebox.showerror("Change Location", "Choose an .exe or .bat launcher.")
        return
    if not os.path.isfile(new_path):
        messagebox.showerror("Change Location", f"The selected file does not exist:\n{new_path}")
        return
    conflict = find_path_conflict(new_path, exclude=item)
    if conflict:
        messagebox.showwarning(
            "Duplicate Location",
            f"That launcher is already registered in {conflict}.",
        )
        return

    old_identity = dict(item.get("identity", {}))
    with data_lock:
        item["path"] = new_path
    if not save_item_library(item):
        with data_lock:
            item["path"] = old_path
            if old_identity:
                item["identity"] = old_identity
        messagebox.showerror("Change Location", "The new location could not be saved.")
        return

    add_activity(
        "location",
        f"Location updated · {item.get('name', 'Launcher')}",
        item,
        detail=f"{ntpath.basename(old_path) or 'missing path'}  →  {ntpath.basename(new_path)}",
        severity="info",
    )
    refresh()
    refresh_item_identity_in_background(item, new_path)
    icon_filename = str(item.get("icon", "") or "")
    icon_path = os.path.join(ICONS_DIR, icon_filename) if icon_filename else ""
    if not icon_path or not os.path.isfile(icon_path):
        extract_icon_in_background(item, new_path)


def invalidate_icon_caches(icon_filename):
    if not icon_filename:
        return
    prefix = f"{icon_filename}-"
    for key in [key for key in icon_cache if str(key).startswith(prefix)]:
        icon_cache.pop(key, None)
    icon_path = os.path.abspath(os.path.join(ICONS_DIR, icon_filename))
    for key in [
        key
        for key in card_art_cache
        if isinstance(key, tuple) and os.path.abspath(str(key[0])) == icon_path
    ]:
        card_art_cache.pop(key, None)


def remove_unreferenced_cached_icon(icon_filename):
    """Delete only an unreferenced file inside XVVIIX's own icon cache."""
    filename = str(icon_filename or "").strip()
    if not filename or os.path.basename(filename) != filename:
        return
    path = os.path.abspath(os.path.join(ICONS_DIR, filename))
    with data_lock:
        referenced = any(
            candidate.get("icon") == filename
            or (
                candidate.get("artwork")
                and os.path.abspath(
                    os.path.expanduser(os.path.expandvars(str(candidate.get("artwork"))))
                    if os.path.isabs(str(candidate.get("artwork")))
                    else os.path.join(BASE_DIR, str(candidate.get("artwork")))
                )
                == path
            )
            for candidate in games + apps + founded
        )
    if referenced:
        return
    cache_root = os.path.abspath(ICONS_DIR) + os.sep
    if not path.startswith(cache_root):
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        LOG.debug("Could not remove unused icon %s: %s", filename, exc)
    invalidate_icon_caches(filename)


def build_custom_icon(source_path, destination_path):
    """Convert an image or Windows executable icon into a safe square PNG."""
    if not HAS_PIL:
        raise RuntimeError("Pillow is required for custom icons")
    source_path = clean_path(source_path)
    extension = ntpath.splitext(source_path)[1].lower()
    temporary_ico = ""
    image_path = source_path
    try:
        if extension != ".exe" and os.path.getsize(source_path) > 64 * 1024 * 1024:
            raise RuntimeError("The selected image is larger than the 64 MB safety limit")
        if extension == ".exe":
            if not HAS_ICOEXTRACT:
                raise RuntimeError(
                    "EXE icon extraction is unavailable; choose an image or .ico file"
                )
            temporary_ico = f"{destination_path}.source-{uuid.uuid4().hex}.ico"
            IconExtractor(source_path).export_icon(temporary_ico)
            if not os.path.isfile(temporary_ico):
                raise RuntimeError("No icon could be extracted from that executable")
            image_path = temporary_ico
        with Image.open(image_path) as source:
            if source.width * source.height > 40_000_000:
                raise RuntimeError("The selected image exceeds the 40-megapixel safety limit")
            source.load()
            image = source.convert("RGBA")
        if not image.width or not image.height:
            raise RuntimeError("The selected image has no usable pixels")
        target = 240
        scale = min(target / image.width, target / image.height)
        resized = image.resize(
            (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
            Image.LANCZOS,
        )
        output = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        output.alpha_composite(
            resized,
            ((256 - resized.width) // 2, (256 - resized.height) // 2),
        )
        temporary_png = f"{destination_path}.tmp-{uuid.uuid4().hex}"
        try:
            output.save(temporary_png, "PNG", optimize=True)
            os.replace(temporary_png, destination_path)
        finally:
            if os.path.exists(temporary_png):
                try:
                    os.remove(temporary_png)
                except OSError:
                    pass
    except Exception as exc:
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
    finally:
        if temporary_ico and os.path.exists(temporary_ico):
            try:
                os.remove(temporary_ico)
            except OSError:
                pass


def finish_custom_icon_update(item_name, error=""):
    if error:
        messagebox.showerror("Add Icon", f"Could not update the icon for {item_name}:\n\n{error}")
    refresh()


def add_custom_icon(item):
    if not HAS_PIL:
        messagebox.showerror("Add Icon", "Install Pillow to use custom icons.")
        return
    item_key = id(item)
    with data_lock:
        if item_key in icon_update_pending:
            messagebox.showinfo("Add Icon", "An icon update is already in progress for this card.")
            return
    selected = filedialog.askopenfilename(
        title=f"Add Icon — {item.get('name', 'Launcher')}",
        filetypes=[
            ("Icon and image files", "*.ico *.png *.jpg *.jpeg *.webp *.bmp"),
            ("Windows executable icon", "*.exe"),
            ("All files", "*.*"),
        ],
    )
    selected = clean_path(selected)
    if not selected:
        return
    if not os.path.isfile(selected):
        messagebox.showerror("Add Icon", f"The selected file does not exist:\n{selected}")
        return
    with data_lock:
        icon_update_pending.add(item_key)

    def worker():
        filename = f"custom_{uuid.uuid4().hex}.png"
        destination = os.path.join(ICONS_DIR, filename)
        old_icon = str(item.get("icon", "") or "")
        error = ""
        try:
            build_custom_icon(selected, destination)
            with data_lock:
                still_present = any(candidate is item for candidate in games + apps + founded)
                if not still_present:
                    raise RuntimeError("The card was removed before the icon update finished")
                item["icon"] = filename
            if not save_item_library(item):
                with data_lock:
                    item["icon"] = old_icon
                raise RuntimeError("the library file could not be saved")
            invalidate_icon_caches(old_icon)
            remove_unreferenced_cached_icon(old_icon)
            add_activity(
                "icon",
                f"Icon updated · {item.get('name', 'Launcher')}",
                item,
                detail=os.path.basename(selected),
                severity="success",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            error = str(exc) or exc.__class__.__name__
            try:
                if os.path.isfile(destination):
                    os.remove(destination)
            except OSError:
                pass
        finally:
            with data_lock:
                icon_update_pending.discard(item_key)
        post_ui(finish_custom_icon_update, item.get("name", "Launcher"), error)

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"custom-icon-{uuid.uuid4().hex[:8]}",
    ).start()


def schedule_icon_refresh():
    global icon_refresh_job
    if root is None:
        return
    if icon_refresh_job is not None:
        try:
            root.after_cancel(icon_refresh_job)
        except tk.TclError:
            pass
    icon_refresh_job = root.after(150, finish_icon_refresh)


def finish_icon_refresh():
    global icon_refresh_job
    icon_refresh_job = None
    refresh()


def _schedule_icon_save():
    global icon_save_job
    if app_exit_event.is_set() or root is None:
        return
    if icon_save_job is not None:
        try:
            root.after_cancel(icon_save_job)
        except tk.TclError:
            pass
    icon_save_job = root.after(700, _dispatch_icon_save)


def _dispatch_icon_save():
    global icon_save_job, icon_save_running
    icon_save_job = None
    if app_exit_event.is_set():
        return
    if icon_save_running:
        icon_save_job = root.after(150, _dispatch_icon_save)
        return
    icon_save_running = True

    def worker():
        global icon_save_running
        try:
            _flush_icon_libraries()
        finally:
            icon_save_running = False

    threading.Thread(target=worker, daemon=True, name="icon-library-save").start()


def _finish_extracted_icon(key, filename):
    request = icon_requests.pop(key, None)
    icon_checked.add(key)
    if request is None or not filename or app_exit_event.is_set():
        return
    item, original_path = request
    changed = False
    with data_lock:
        if canonical_path(item.get("path", "")) != original_path or icons.is_user_icon(
            item.get("icon", "")
        ):
            return
        for name, library in (("games", games), ("apps", apps), ("founded", founded)):
            if any(candidate is item for candidate in library):
                if item.get("icon") != filename:
                    old_icon = item.get("icon", "")
                    item["icon"] = filename
                    icon_dirty_versions[name] += 1
                    dirty_icon_libraries.add(name)
                    invalidate_icon_caches(old_icon)
                    changed = True
                break
    if changed:
        schedule_icon_refresh()
        _schedule_icon_save()


def extract_icon_in_background(item, path):
    """At most one bounded extractor; keep native ICO frames and custom icons."""
    global icon_worker
    if not HAS_PIL or app_exit_event.is_set() or icons.is_user_icon(item.get("icon", "")):
        return
    original_path = canonical_path(path)
    key = (id(item), original_path)
    if key in icon_checked or key in icon_requests:
        return
    if icon_worker is None:
        icon_worker = artwork.ArtworkWorker(
            icons.extract_native_icon,
            lambda request_key, filename: post_ui(_finish_extracted_icon, request_key, filename),
            capacity=96,
            logger=LOG,
            thread_name="icon-extractor",
        )
    icon_requests[key] = (item, original_path)
    if not icon_worker.submit(key, path, ICONS_DIR):
        icon_requests.pop(key, None)


def add_item(forced_path=None):
    if active_tab not in ("games", "apps", "founded"):
        return
    name = name_entry.get().strip()
    path = forced_path or filedialog.askopenfilename(
        title="Select Program or Script",
        filetypes=[
            ("EXE and BAT files", "*.exe *.bat"),
            ("Executable files", "*.exe"),
            ("Batch files", "*.bat"),
            ("All files", "*.*"),
        ],
    )
    path = clean_path(path)
    if not path:
        return
    if ntpath.splitext(path)[1].lower() not in (".exe", ".bat"):
        messagebox.showerror("Unsupported file", "Only .exe and .bat files can be added.")
        return
    if library_has_path(current_list(), path):
        messagebox.showwarning("Duplicate", "This executable is already in the current library.")
        return
    if not name:
        name = ntpath.splitext(ntpath.basename(path))[0]
    trainer = ""
    if active_tab == "games":
        trainer = (
            filedialog.askopenfilename(
                title="Select Trainer (optional)", filetypes=[("Executable", "*.exe")]
            )
            or ""
        )

    item = {
        "name": name,
        "path": path,
        "trainer": trainer,
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color(),
    }
    with data_lock:
        current_list().append(item)
    save_current()
    name_entry.delete(0, tk.END)
    refresh()
    extract_icon_in_background(item, path)


def add_item_from_path(path):
    if active_tab not in ("games", "apps", "founded"):
        return
    path = clean_path(path)
    if not path or ntpath.splitext(path)[1].lower() not in (".exe", ".bat"):
        messagebox.showinfo("Unsupported file", f"Cannot add: {path}")
        return
    if library_has_path(current_list(), path):
        messagebox.showwarning("Duplicate", "This executable is already in the current library.")
        return
    item = {
        "name": ntpath.splitext(ntpath.basename(path))[0],
        "path": path,
        "trainer": "",
        "icon": "",
        "playtime": 0,
        "pinned": False,
        "color": random_color(),
    }
    with data_lock:
        current_list().append(item)
    save_current()
    refresh()
    extract_icon_in_background(item, path)


def default_drop_text():
    if DND_ACTIVE:
        return "＋  DRAG & DROP  .EXE  /  .BAT  /  .LNK   ·   ADDS TO CURRENT LIBRARY"
    return "DRAG & DROP UNAVAILABLE   ·   INSTALL tkinterdnd2"


def set_drop_status(text, foreground=SUBTEXT, background=BG2):
    global drop_reset_job
    if drop_zone is None or root is None:
        return
    try:
        drop_zone.configure(text=text)
        target_bg = BG2 if background == BG2 else lerp_color(BG2, background, 0.30)
        animate_widget_color(drop_zone, "bg", target_bg, 180, 10, CARD)
        animate_widget_color(drop_zone, "fg", foreground, 180, 10, SUBTEXT)
        animate_widget_color(
            drop_zone,
            "highlightbackground",
            BORDER if background == CARD else background,
            180,
            10,
            BORDER,
        )
        if drop_reset_job is not None:
            root.after_cancel(drop_reset_job)
        drop_reset_job = root.after(3000, reset_drop_status)
    except tk.TclError:
        pass


def reset_drop_status():
    global drop_reset_job
    drop_reset_job = None
    if drop_zone is None:
        return
    try:
        drop_zone.configure(text=default_drop_text())
        animate_widget_color(drop_zone, "bg", BG2, 220, 11, BG2)
        animate_widget_color(drop_zone, "fg", SUBTEXT, 220, 11, SUBTEXT)
        animate_widget_color(drop_zone, "highlightbackground", BORDER, 220, 11, BORDER)
    except tk.TclError:
        pass


def add_dropped_paths(paths):
    """Add a group of dropped launchers with one save and one UI refresh."""
    if active_tab not in ("games", "apps", "founded"):
        return 0, 0, len(paths)
    target = current_list()
    existing = {canonical_path(item.get("path", "")) for item in target if item.get("path")}
    new_items = []
    rejected = 0
    duplicates = 0

    for raw_path in paths:
        path = clean_path(str(raw_path).strip("{}"))
        if path.lower().endswith(".lnk"):
            path = clean_path(resolve_shortcut(path))
        extension = ntpath.splitext(path)[1].lower()
        if extension not in (".exe", ".bat") or not os.path.isfile(path):
            rejected += 1
            continue
        key = canonical_path(path)
        if not key or key in existing:
            duplicates += 1
            continue
        item = {
            "name": ntpath.splitext(ntpath.basename(path))[0],
            "path": path,
            "trainer": "",
            "icon": "",
            "playtime": 0,
            "pinned": False,
            "color": random_color(),
        }
        new_items.append(item)
        existing.add(key)

    if new_items:
        with data_lock:
            target.extend(new_items)
        save_current()
        refresh(reset_page=True)
        for item in new_items:
            extract_icon_in_background(item, item["path"])

    return len(new_items), duplicates, rejected


def handle_drop_event(event):
    try:
        paths = root.tk.splitlist(event.data)
    except (tk.TclError, AttributeError):
        paths = ()
    added, duplicates, rejected = add_dropped_paths(paths)
    details = []
    if duplicates:
        details.append(f"{duplicates} duplicate")
    if rejected:
        details.append(f"{rejected} unsupported")
    suffix = f" — {', '.join(details)}" if details else ""
    if added:
        set_drop_status(f"✓ ADDED {added} ITEM{'S' if added != 1 else ''}{suffix}", TEXT, GREEN)
    else:
        set_drop_status(f"NO FILES ADDED{suffix}", TEXT, ORANGE)
    return getattr(event, "action", "copy")


def handle_drop_enter(event):
    set_drop_status("RELEASE TO ADD TO THIS LIBRARY", TEXT, ACCENT)
    return getattr(event, "action", "copy")


def handle_drop_leave(event):
    reset_drop_status()
    return getattr(event, "action", "copy")


# ==========================================
# PROCESS TRACKING
# ==========================================


def item_library_kind(item):
    with data_lock:
        if any(candidate is item for candidate in games):
            return "game"
        if any(candidate is item for candidate in apps):
            return "app"
    return "unknown"


def finalize_process_session(pid, session):
    """Finish playtime/activity tracking without diagnosing or reporting crashes."""
    if session.get("launcher_shutdown") or session.get("finalized"):
        return
    session["finalized"] = True
    item = session.get("item") if isinstance(session.get("item"), dict) else {}
    ended = float(session.get("ended_at") or time.time())
    started = float(session.get("started_at") or session.get("created") or ended)
    runtime_seconds = max(0, int(ended - started))
    manual = bool(session.get("end_requested") or session.get("user_ended"))
    add_activity(
        "end_task" if manual else "closed",
        f"Task ended · {item.get('name', 'Application')}"
        if manual
        else f"Session closed · {item.get('name', 'Application')}",
        item,
        detail=f"PID {pid} · {format_time(runtime_seconds)} session",
        severity="warning" if manual else "info",
        epoch=ended,
    )


def register_process(item, process, executable_path, started_at=None):
    if app_exit_event.is_set():
        if hasattr(process, "detach"):
            process.detach()
        return
    now = time.time() if started_at is None else started_at
    created = now
    if HAS_PSUTIL:
        try:
            created = float(psutil.Process(process.pid).create_time())
        except (psutil.Error, OSError, ValueError):
            pass
    library_kind = item_library_kind(item)
    with process_lock:
        tracked_processes[process.pid] = {
            "item": item,
            "path": canonical_path(executable_path),
            "created": created,
            "started_at": now,
            "last_accounted": now,
            "ended_at": None,
            "exit_code": None,
            "library_kind": library_kind,
            "end_requested": False,
            "user_ended": False,
            "process": process,
        }
    add_activity(
        "launch",
        f"Launched · {item.get('name', 'Application')}",
        item,
        detail=f"PID {process.pid} · {library_kind.upper()}",
        severity="success",
        epoch=now,
    )
    if root is not None:
        post_ui(refresh)
    threading.Thread(
        target=wait_for_direct_process,
        args=(process.pid, process),
        daemon=True,
        name=f"process-wait-{process.pid}",
    ).start()


def wait_for_direct_process(pid, process):
    exit_code = None
    try:
        exit_code = process.wait()
    except OSError:
        pass
    ended_at = time.time()
    with process_lock:
        session = tracked_processes.get(pid)
        if session is not None and session.get("process") is process:
            session["ended_at"] = ended_at
            session["exit_code"] = exit_code
    if not app_exit_event.is_set():
        account_tracked_processes()


def _process_is_alive(pid, session):
    if session.get("ended_at") is not None:
        return False
    direct_process = session.get("process")
    if direct_process is not None:
        return direct_process.poll() is None
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - session["created"]) > 0.01:
            return False
        executable = process.exe()
        return not session["path"] or canonical_path(executable) == session["path"]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


def account_tracked_processes(remove_all=False, persist=True, cutoff_time=None):
    """Read OS state without locks; keep model locks short and in data->process order."""
    now = time.time() if cutoff_time is None else cutoff_time
    changed, finished_sessions = False, []
    with process_lock:
        observed = [(pid, session, dict(session)) for pid, session in tracked_processes.items()]
    alive_by_pid = {}
    if not remove_all:
        for pid, _session, snapshot in observed:
            if app_exit_event.is_set():
                return False, False
            alive_by_pid[pid] = _process_is_alive(pid, snapshot)
    with data_lock, process_lock:
        game_ids, app_ids = {id(item) for item in games}, {id(item) for item in apps}
        for pid, session, _snapshot in observed:
            if tracked_processes.get(pid) is not session:
                continue
            alive = (
                not remove_all and alive_by_pid.get(pid, False) and session.get("ended_at") is None
            )
            cutoff = min(now, session.get("ended_at") or now)
            elapsed = max(0, int(cutoff - session["last_accounted"]))
            if elapsed:
                item = session["item"]
                item["playtime"] = max(0, int(item.get("playtime", 0))) + elapsed
                session["last_accounted"] += elapsed
                if id(item) in game_ids:
                    dirty_playtime_libraries.add("games")
                elif id(item) in app_ids:
                    dirty_playtime_libraries.add("apps")
                changed = True
            if not alive:
                tracked_processes.pop(pid, None)
                if session.get("ended_at") is None:
                    session["ended_at"] = now
                if remove_all:
                    session["launcher_shutdown"] = True
                finished_sessions.append((pid, session))
    persist_now = persist or bool(finished_sessions) or remove_all
    persisted = False
    if persist_now:
        for name, filepath, library in (
            ("games", vault.GAMES_FILE, games),
            ("apps", vault.APPS_FILE, apps),
        ):
            if name in dirty_playtime_libraries and vault._save(filepath, library):
                dirty_playtime_libraries.discard(name)
                persisted = True
    for pid, session in finished_sessions:
        if remove_all and hasattr(session.get("process"), "detach"):
            session["process"].detach()
        finalize_process_session(pid, session)
    if (changed and persist_now) or persisted or finished_sessions:
        post_ui(refresh)
    return changed, persisted


def monitor_running_apps():
    if not HAS_PSUTIL:
        return
    last_persist = time.monotonic()
    while not monitor_stop.wait(5):
        path_index = {}
        with data_lock:
            for library_kind, library in (("game", games), ("app", apps)):
                for item in library:
                    normalized = canonical_path(item.get("path", ""))
                    if normalized:
                        path_index.setdefault(normalized, (item, library_kind))
        if not path_index:
            # There is nothing to discover. Still settle any already tracked sessions.
            persist_now = time.monotonic() - last_persist >= 30
            account_tracked_processes(persist=persist_now)
            if persist_now:
                last_persist = time.monotonic()
            continue
        detected_sessions = []
        try:
            for process in psutil.process_iter(["pid", "exe", "create_time"]):
                try:
                    executable = process.info.get("exe")
                    match = path_index.get(canonical_path(executable)) if executable else None
                    if match is None:
                        continue
                    item, library_kind = match
                    with process_lock:
                        if app_exit_event.is_set():
                            return
                        if process.pid not in tracked_processes:
                            created = float(process.info.get("create_time") or time.time())
                            tracked_processes[process.pid] = {
                                "item": item,
                                "path": canonical_path(executable),
                                "created": created,
                                "started_at": created,
                                "last_accounted": max(created, time.time() - 5),
                                "ended_at": None,
                                "exit_code": None,
                                "library_kind": library_kind,
                                "end_requested": False,
                                "user_ended": False,
                                "process": None,
                            }
                            detected_sessions.append((process.pid, item, library_kind, created))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    continue
            for pid, item, library_kind, _created in detected_sessions:
                add_activity(
                    "detected",
                    f"Running · {item.get('name', 'Application')}",
                    item,
                    detail=f"PID {pid} · {library_kind.upper()} detected",
                    severity="success",
                    epoch=time.time(),
                )
            if detected_sessions:
                post_ui(refresh)
            persist_now = time.monotonic() - last_persist >= 30
            account_tracked_processes(persist=persist_now)
            if persist_now:
                last_persist = time.monotonic()
        except (psutil.Error, OSError) as exc:
            LOG.warning("Process monitor cycle failed: %s", exc)


def running_sessions_for_item(item):
    with process_lock:
        return [
            (pid, session)
            for pid, session in tracked_processes.items()
            if session.get("item") is item and session.get("ended_at") is None
        ]


def is_item_running(item):
    return bool(running_sessions_for_item(item))


def _terminate_tracked_session(pid, session):
    return termination.terminate_session(pid, session, module=psutil if HAS_PSUTIL else None)


def finish_end_task_ui(item_name, ended_count, failures):
    refresh()
    if not failures:
        return
    detail = "\n".join(f"PID {entry[0]}: {entry[1]}" for entry in failures[:5])
    denied = any(len(entry) > 2 and entry[2] for entry in failures)
    hint = (
        "\n\nSome remaining processes denied access. Administrator permission may be needed for those processes."
        if denied
        else ""
    )
    if ended_count:
        messagebox.showwarning(
            "End Task partially completed",
            f"Stopped {ended_count} process(es) for {item_name}.\n\n{detail}{hint}",
        )
    else:
        messagebox.showerror(
            "End Task incomplete",
            f"Some processes for {item_name} are still running or could not be verified.\n\n{detail}{hint}",
        )


def end_task(item):
    sessions = running_sessions_for_item(item)
    if not sessions:
        messagebox.showinfo(
            "End Task", f"{item.get('name', 'This item')} is not currently running."
        )
        refresh()
        return
    if not messagebox.askyesno(
        "End Task",
        f"End {item.get('name', 'this task')} and its child processes?\n\nUnsaved progress may be lost.",
        icon="warning",
    ):
        return
    with process_lock:
        for _pid, session in sessions:
            session["end_requested"] = True
    refresh()

    def worker():
        ended_count = 0
        failures = []
        for pid, session in sessions:
            try:
                _terminate_tracked_session(pid, session)
                with process_lock:
                    session["user_ended"] = True
                    if session.get("ended_at") is None:
                        session["ended_at"] = time.time()
                ended_count += 1
            except RuntimeError as exc:
                with process_lock:
                    session["end_requested"] = False
                failures.append((pid, str(exc), bool(getattr(exc, "permission_denied", False))))
        account_tracked_processes()
        post_ui(finish_end_task_ui, item.get("name", "Application"), ended_count, failures)

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"end-task-{canonical_path(item.get('path', ''))[-24:] or 'process'}",
    ).start()


def is_item_launching(item):
    with launch_lock:
        return canonical_path(item.get("path", "")) in pending_launches


def _launch_feedback(name, error, cancelled=False):
    if app_exit_event.is_set():
        return
    refresh()
    if error:
        add_activity(
            "launch" if cancelled else "error",
            f"Launch cancelled · {name}" if cancelled else f"Launch failed · {name}",
            detail=error,
            severity="info" if cancelled else "error",
        )
        callback = messagebox.showinfo if cancelled else messagebox.showerror
        callback("Launch cancelled" if cancelled else "Launch Error", f"{name}\n\n{error}")


def _begin_item_launch(item, with_trainer=False):
    if app_exit_event.is_set():
        return False
    path = clean_path(item.get("path", ""))
    key = canonical_path(path)
    with launch_lock:
        if key in pending_launches:
            return False
        acquired = launch_slots.acquire(blocking=False)
        if acquired:
            pending_launches.add(key)
    if not acquired:
        messagebox.showinfo(
            "Launch queue", "Several programs are already starting. Please wait a moment."
        )
        return False
    trainer = clean_path(item.get("trainer", "")) if with_trainer else ""
    name = item.get("name", "Application")
    post_ui(refresh)

    def worker():
        error, cancelled = "", False
        try:
            result = launching.launch_program(path, trainer, cancel=app_exit_event)
            if not app_exit_event.is_set():
                if result.process is not None:
                    register_process(item, result.process, result.path, result.started_at)
                else:
                    with launch_lock:
                        shell_launches[key] = result.started_at
                    add_activity(
                        "launch",
                        f"Launch requested · {name}",
                        item,
                        detail="Windows shell accepted the launch; waiting for process detection.",
                    )
            elif result.process is not None and hasattr(result.process, "detach"):
                result.process.detach()
        except launching.LaunchCancelled as exc:
            error, cancelled = str(exc), True
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            LOG.warning("Launch failed for %s: %s", path, error)
        finally:
            with launch_lock:
                pending_launches.discard(key)
            launch_slots.release()
            post_ui(_launch_feedback, name, error, cancelled)

    threading.Thread(target=worker, daemon=True, name="program-launcher").start()
    return True


def run_only(item):
    return _begin_item_launch(item)


def run_with_trainer(item):
    return _begin_item_launch(item, with_trainer=True)


def _flush_icon_libraries():
    for name, filepath, library in (
        ("games", vault.GAMES_FILE, games),
        ("apps", vault.APPS_FILE, apps),
        ("founded", vault.FOUNDED_FILE, founded),
    ):
        with data_lock:
            needed = name in dirty_icon_libraries or name in dirty_scan_libraries
            version = icon_dirty_versions[name]
        if needed and vault._save(filepath, library):
            with data_lock:
                if icon_dirty_versions[name] == version:
                    dirty_icon_libraries.discard(name)
                dirty_scan_libraries.discard(name)


def _checkpoint_before_exit():
    account_tracked_processes(remove_all=True, persist=True, cutoff_time=shutdown_started_at)
    _flush_icon_libraries()
    return [
        f"Could not save {name}"
        for name in sorted(dirty_playtime_libraries | dirty_icon_libraries | dirty_scan_libraries)
    ]


def _stop_background_services():
    """Signal/cancel owned work; do not join game waiters or block Tk on drivers."""
    global background_shutdown_started, scan_cancel
    if background_shutdown_started:
        return
    background_shutdown_started = True
    monitor_stop.set()
    scan_cancel = True
    sys_report_cancel.set()
    if icon_worker is not None:
        icon_worker.stop()
    stop_card_artwork()
    if activity_history is not None:
        activity_history.destroy()
    close_hardware_overlay()
    cancel_hardware_monitor_idle_stop()
    cancel_hardware_monitor_view_refresh()
    cancel_all_animations()

    def cleanup(service):
        try:
            service.stop()
        except Exception as exc:
            LOG.debug("Background service shutdown: %s", exc)

    for service in (hardware_monitor, background_music):
        if service is not None:
            threading.Thread(
                target=cleanup, args=(service,), daemon=True, name="service-cleanup"
            ).start()


def _finish_launcher_close(gave_up=False):
    if gave_up:
        LOG.warning("User chose to exit without waiting for the latest save")
    vault.clear_vault_key()
    try:
        cancel_pending_callbacks(root)
        root.destroy()
    except (AttributeError, tk.TclError):
        pass


def request_launcher_close():
    global shutdown_dialog, shutdown_started_at
    if app_exit_event.is_set():
        if shutdown_dialog is not None and widget_exists(shutdown_dialog.window):
            shutdown_dialog.window.lift()
        return False
    with process_lock:
        active = sum(session.get("ended_at") is None for session in tracked_processes.values())
    with launch_lock:
        pending = len(pending_launches)
        recent_shell = any(time.time() - started < 30 for started in shell_launches.values())
    if active or pending or recent_shell:
        if not messagebox.askyesno(
            "Close launcher and stop time tracking?",
            "Programs may still be running or starting. They will stay open.\n\n"
            "After the launcher closes, playtime will no longer be recorded. "
            "Reopening it later cannot accurately recover that missing time.\n\nClose the launcher?",
            parent=root,
            icon="warning",
        ):
            return False
    elif scan_running or sys_report_running:
        if not messagebox.askyesno(
            "Close launcher?", "The current scan will be cancelled. Close XVVIIX?", parent=root
        ):
            return False
    shutdown_started_at = time.time()
    app_exit_event.set()
    startup_checkpoint(
        "SHUTDOWN_REQUEST", "STARTED", "checkpointing timing; running programs are not terminated"
    )
    _stop_background_services()
    shutdown_dialog = ClosingDialog(root, _checkpoint_before_exit, _finish_launcher_close)
    return True


# ==========================================
# SYSTEM REPORT
# ==========================================
class SystemReportCancelled(Exception):
    pass


def _system_report_progress(callback, percent, phase, detail=""):
    if sys_report_cancel.is_set():
        raise SystemReportCancelled()
    if callback:
        callback(max(0, min(100, int(percent))), phase, detail)


def _powershell_system_inventory():
    if os.name != "nt":
        return {}
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        return {}
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem
$os=Get-CimInstance Win32_OperatingSystem
$bios=Get-CimInstance Win32_BIOS
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu=@(Get-CimInstance Win32_VideoController | ForEach-Object {
  [pscustomobject]@{Name=$_.Name;DriverVersion=$_.DriverVersion;AdapterRAM=$_.AdapterRAM;Status=$_.Status}
})
$def=Get-MpComputerStatus
$fw=@(Get-NetFirewallProfile | ForEach-Object {[pscustomobject]@{Name=$_.Name;Enabled=$_.Enabled}})
$bad=@(Get-CimInstance Win32_PnPEntity | Where-Object {$_.ConfigManagerErrorCode -ne 0} | Select-Object -First 20 Name,ConfigManagerErrorCode)
$tpm=Get-Tpm
[pscustomobject]@{
 Computer=[pscustomobject]@{Manufacturer=$cs.Manufacturer;Model=$cs.Model;TotalPhysicalMemory=$cs.TotalPhysicalMemory}
 OS=[pscustomobject]@{Caption=$os.Caption;Version=$os.Version;BuildNumber=$os.BuildNumber;InstallDate=$os.InstallDate}
 BIOS=[pscustomobject]@{Manufacturer=$bios.Manufacturer;Version=($bios.SMBIOSBIOSVersion -join ', ');ReleaseDate=$bios.ReleaseDate}
 CPU=[pscustomobject]@{Name=$cpu.Name;MaxClockSpeed=$cpu.MaxClockSpeed;Cores=$cpu.NumberOfCores;Logical=$cpu.NumberOfLogicalProcessors}
 GPU=$gpu
 Defender=[pscustomobject]@{AntivirusEnabled=$def.AntivirusEnabled;RealTimeProtectionEnabled=$def.RealTimeProtectionEnabled;SignaturesOutOfDate=$def.AntivirusSignatureOutOfDate}
 Firewall=$fw
 DeviceErrors=$bad
 TPM=[pscustomobject]@{Present=$tpm.TpmPresent;Ready=$tpm.TpmReady;Enabled=$tpm.TpmEnabled}
} | ConvertTo-Json -Depth 6 -Compress
"""
    process = None
    try:
        process = subprocess.Popen(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 15
        while True:
            if sys_report_cancel.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise SystemReportCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                return {}
            try:
                stdout, _stderr = process.communicate(timeout=min(0.15, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        output = stdout.strip()
        if not output or len(output) > 2 * 1024 * 1024:
            return {}
        return json.loads(output)
    except SystemReportCancelled:
        raise
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeError) as exc:
        LOG.debug("Windows system inventory unavailable: %s", exc)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        return {}


def _cpu_model_name():
    name = platform.processor().strip()
    if name:
        return name
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.casefold().startswith("model name") and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.machine() or "Unknown processor"


def _as_record_list(value):
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def collect_system_report(progress_callback=None):
    """Collect a bounded local hardware, OS, storage, network, and security report."""
    started = time.perf_counter()
    epoch = time.time()
    _system_report_progress(
        progress_callback, 3, "INITIALIZING", "Building local diagnostic inventory"
    )
    hostname = socket.gethostname() or platform.node() or "Unknown device"
    os_name = platform.system() or "Unknown OS"
    os_release = platform.release()
    os_version = platform.version()
    architecture = platform.machine() or "Unknown"
    python_version = platform.python_version()
    sections = []
    findings = []
    score = 100

    def finding(severity, title, detail, action="", penalty=0):
        nonlocal score
        findings.append(
            {
                "severity": severity,
                "title": title,
                "detail": detail,
                "action": action,
            }
        )
        score = max(0, score - max(0, int(penalty)))

    _system_report_progress(
        progress_callback, 12, "PLATFORM", "Reading operating system and firmware identity"
    )
    windows = _powershell_system_inventory()
    computer = windows.get("Computer") if isinstance(windows.get("Computer"), dict) else {}
    windows_os = windows.get("OS") if isinstance(windows.get("OS"), dict) else {}
    bios = windows.get("BIOS") if isinstance(windows.get("BIOS"), dict) else {}
    manufacturer = str(computer.get("Manufacturer") or "Unknown")
    model = str(computer.get("Model") or platform.node() or "Unknown")
    os_label = str(windows_os.get("Caption") or f"{os_name} {os_release}").strip()
    build = str(windows_os.get("BuildNumber") or os_version or "Unknown")
    boot_epoch = 0.0
    uptime_seconds = 0
    if HAS_PSUTIL:
        try:
            boot_epoch = float(psutil.boot_time())
            uptime_seconds = max(0, int(time.time() - boot_epoch))
        except (psutil.Error, OSError, ValueError):
            pass
    platform_items = [
        {"label": "HOSTNAME", "value": hostname, "status": "success"},
        {"label": "DEVICE", "value": f"{manufacturer} {model}".strip(), "status": "info"},
        {"label": "OPERATING SYSTEM", "value": os_label, "status": "success"},
        {"label": "BUILD / KERNEL", "value": build, "status": "info"},
        {"label": "ARCHITECTURE", "value": architecture, "status": "info"},
        {
            "label": "UPTIME",
            "value": format_time(uptime_seconds),
            "status": "warning" if uptime_seconds > 14 * 86400 else "success",
        },
    ]
    if bios:
        platform_items.append(
            {
                "label": "BIOS",
                "value": f"{bios.get('Manufacturer') or ''} {bios.get('Version') or ''}".strip()
                or "Unknown",
                "status": "info",
            }
        )
    sections.append({"title": "01  COMMAND PLATFORM", "status": "success", "items": platform_items})
    if uptime_seconds > 14 * 86400:
        finding(
            "warning",
            "Extended system uptime",
            f"The computer has been running for {format_time(uptime_seconds)}.",
            "Restart Windows before diagnosing intermittent game or driver problems.",
            4,
        )

    _system_report_progress(
        progress_callback, 27, "COMPUTE CORE", "Sampling processor topology and load"
    )
    cpu_model = _cpu_model_name()
    cpu_physical = os.cpu_count() or 0
    cpu_logical = os.cpu_count() or 0
    cpu_percent = 0.0
    frequency = 0.0
    process_count = 0
    if HAS_PSUTIL:
        try:
            cpu_physical = psutil.cpu_count(logical=False) or cpu_physical
            cpu_logical = psutil.cpu_count(logical=True) or cpu_logical
            cpu_percent = float(psutil.cpu_percent(interval=0.25))
            freq = psutil.cpu_freq()
            frequency = float(freq.max or freq.current or 0.0) if freq else 0.0
            process_count = len(psutil.pids())
        except (psutil.Error, OSError, ValueError):
            pass
    win_cpu = windows.get("CPU") if isinstance(windows.get("CPU"), dict) else {}
    cpu_model = str(win_cpu.get("Name") or cpu_model).strip()
    frequency = float(win_cpu.get("MaxClockSpeed") or frequency or 0.0)
    cpu_physical = int(win_cpu.get("Cores") or cpu_physical or 0)
    cpu_logical = int(win_cpu.get("Logical") or cpu_logical or 0)
    cpu_status = (
        "critical" if cpu_percent >= 95 else ("warning" if cpu_percent >= 85 else "success")
    )
    sections.append(
        {
            "title": "02  COMPUTE CORE",
            "status": cpu_status,
            "items": [
                {"label": "PROCESSOR", "value": cpu_model, "status": "info"},
                {
                    "label": "TOPOLOGY",
                    "value": f"{cpu_physical} physical / {cpu_logical} logical cores",
                    "status": "success",
                },
                {
                    "label": "MAX FREQUENCY",
                    "value": f"{frequency / 1000:.2f} GHz" if frequency else "Unavailable",
                    "status": "info",
                },
                {
                    "label": "UTILIZATION SAMPLE",
                    "value": f"{cpu_percent:.1f}%",
                    "status": cpu_status,
                },
                {
                    "label": "ACTIVE PROCESSES",
                    "value": str(process_count) if process_count else "Unavailable",
                    "status": "info",
                },
            ],
        }
    )
    if cpu_percent >= 95:
        finding(
            "critical",
            "Processor saturation",
            f"CPU utilization sampled at {cpu_percent:.1f}%.",
            "Close CPU-heavy background tasks and scan for runaway processes.",
            12,
        )
    elif cpu_percent >= 85:
        finding(
            "warning",
            "High processor load",
            f"CPU utilization sampled at {cpu_percent:.1f}%.",
            "Repeat the report after closing background workloads.",
            6,
        )

    _system_report_progress(
        progress_callback, 42, "MEMORY", "Measuring physical and virtual memory pressure"
    )
    memory_total = memory_available = memory_used = 0
    memory_percent = swap_percent = 0.0
    if HAS_PSUTIL:
        try:
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            memory_total = int(memory.total)
            memory_available = int(memory.available)
            memory_used = int(memory.used)
            memory_percent = float(memory.percent)
            swap_percent = float(swap.percent)
        except (psutil.Error, OSError, ValueError):
            pass
    if not memory_total:
        try:
            memory_total = int(computer.get("TotalPhysicalMemory") or 0)
        except (TypeError, ValueError):
            memory_total = 0
    memory_status = (
        "critical" if memory_percent >= 95 else ("warning" if memory_percent >= 85 else "success")
    )
    sections.append(
        {
            "title": "03  MEMORY ARRAY",
            "status": memory_status,
            "items": [
                {"label": "INSTALLED", "value": format_bytes(memory_total), "status": "info"},
                {"label": "IN USE", "value": format_bytes(memory_used), "status": memory_status},
                {
                    "label": "AVAILABLE",
                    "value": format_bytes(memory_available),
                    "status": memory_status,
                },
                {
                    "label": "MEMORY LOAD",
                    "value": f"{memory_percent:.1f}%",
                    "status": memory_status,
                },
                {
                    "label": "SWAP LOAD",
                    "value": f"{swap_percent:.1f}%",
                    "status": "warning" if swap_percent >= 75 else "info",
                },
            ],
        }
    )
    if memory_percent >= 95:
        finding(
            "critical",
            "Critical memory pressure",
            f"Physical memory usage is {memory_percent:.1f}%.",
            "Close memory-heavy applications or increase available RAM.",
            15,
        )
    elif memory_percent >= 85:
        finding(
            "warning",
            "High memory pressure",
            f"Physical memory usage is {memory_percent:.1f}%.",
            "Close unused applications before launching a game.",
            7,
        )

    _system_report_progress(
        progress_callback, 56, "GRAPHICS", "Inspecting graphics adapters and drivers"
    )
    gpu_records = _as_record_list(windows.get("GPU"))
    graphics_items = []
    gpu_names = []
    for index, gpu in enumerate(gpu_records[:6], start=1):
        name = str(gpu.get("Name") or f"Graphics adapter {index}")
        gpu_names.append(name)
        try:
            vram = int(gpu.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            vram = 0
        value = name
        if vram:
            value += f" · {format_bytes(vram)}"
        if gpu.get("DriverVersion"):
            value += f" · DRIVER {gpu.get('DriverVersion')}"
        graphics_items.append({"label": f"ADAPTER {index}", "value": value, "status": "success"})
    if not graphics_items:
        graphics_items.append(
            {"label": "ADAPTER", "value": "Detailed GPU inventory unavailable", "status": "info"}
        )
    sections.append(
        {
            "title": "04  GRAPHICS SUBSYSTEM",
            "status": "success" if gpu_names else "info",
            "items": graphics_items,
        }
    )

    _system_report_progress(
        progress_callback, 68, "STORAGE", "Mapping local volumes and capacity margins"
    )
    storage_items = []
    storage_total = storage_free = 0
    if HAS_PSUTIL:
        seen_mounts = set()
        try:
            for partition in psutil.disk_partitions(all=False)[:20]:
                mount = partition.mountpoint
                if mount in seen_mounts:
                    continue
                seen_mounts.add(mount)
                try:
                    usage = psutil.disk_usage(mount)
                except (PermissionError, psutil.Error, OSError):
                    continue
                storage_total += int(usage.total)
                storage_free += int(usage.free)
                free_percent = 100.0 - float(usage.percent)
                status = (
                    "critical"
                    if free_percent < 5
                    else ("warning" if free_percent < 12 else "success")
                )
                storage_items.append(
                    {
                        "label": mount,
                        "value": f"{format_bytes(usage.free)} free / {format_bytes(usage.total)} · {usage.percent:.1f}% used",
                        "status": status,
                    }
                )
                if free_percent < 5:
                    finding(
                        "critical",
                        f"Critical storage margin on {mount}",
                        f"Only {free_percent:.1f}% remains free.",
                        "Free disk space before installing or updating games.",
                        15,
                    )
                elif free_percent < 12:
                    finding(
                        "warning",
                        f"Low storage margin on {mount}",
                        f"Only {free_percent:.1f}% remains free.",
                        "Remove temporary files or move unused games.",
                        7,
                    )
        except (psutil.Error, OSError):
            pass
    if not storage_items:
        storage_items.append(
            {"label": "VOLUMES", "value": "Storage inventory unavailable", "status": "warning"}
        )
    storage_status = (
        "critical"
        if any(item["status"] == "critical" for item in storage_items)
        else (
            "warning" if any(item["status"] == "warning" for item in storage_items) else "success"
        )
    )
    sections.append(
        {"title": "05  STORAGE MODULES", "status": storage_status, "items": storage_items}
    )

    _system_report_progress(
        progress_callback, 78, "NETWORK", "Enumerating active local network links"
    )
    network_items = []
    active_interfaces = []
    ipv4_addresses = []
    if HAS_PSUTIL:
        try:
            stats = psutil.net_if_stats()
            addresses = psutil.net_if_addrs()
            for name, stat in list(stats.items())[:24]:
                if not stat.isup:
                    continue
                active_interfaces.append(name)
                speed = (
                    f"{stat.speed} Mbps" if stat.speed and stat.speed > 0 else "speed unavailable"
                )
                ips = [
                    address.address
                    for address in addresses.get(name, [])
                    if address.family == socket.AF_INET and not address.address.startswith("127.")
                ]
                ipv4_addresses.extend(ips)
                value = speed + (f" · {', '.join(ips[:3])}" if ips else "")
                network_items.append({"label": name, "value": value, "status": "success"})
        except (psutil.Error, OSError, ValueError):
            pass
    if not network_items:
        network_items.append(
            {
                "label": "LINK STATUS",
                "value": "No active interface details available",
                "status": "warning",
            }
        )
    sections.append(
        {
            "title": "06  NETWORK LINKS",
            "status": "success" if active_interfaces else "warning",
            "items": network_items,
        }
    )

    _system_report_progress(
        progress_callback,
        87,
        "POWER / SECURITY",
        "Checking battery, thermal, firewall, and protection state",
    )
    power_items = []
    battery_percent = None
    battery_plugged = None
    temperatures = []
    if HAS_PSUTIL:
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_percent = float(battery.percent)
                battery_plugged = bool(battery.power_plugged)
                power_items.append(
                    {
                        "label": "BATTERY",
                        "value": f"{battery_percent:.0f}% · {'AC POWER' if battery_plugged else 'ON BATTERY'}",
                        "status": "warning"
                        if battery_percent < 20 and not battery_plugged
                        else "success",
                    }
                )
        except (psutil.Error, OSError, ValueError, AttributeError):
            pass
        try:
            sensor_map = (
                psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
            )
            for group, entries in list(sensor_map.items())[:8]:
                for entry in entries[:4]:
                    if entry.current is not None:
                        temperatures.append((entry.label or group, float(entry.current)))
        except (psutil.Error, OSError, ValueError, AttributeError):
            pass
    for label, temperature in temperatures[:8]:
        status = (
            "critical" if temperature >= 90 else ("warning" if temperature >= 80 else "success")
        )
        power_items.append(
            {"label": label.upper(), "value": f"{temperature:.1f} °C", "status": status}
        )
        if temperature >= 90:
            finding(
                "critical",
                "Critical thermal reading",
                f"{label} reported {temperature:.1f} °C.",
                "Stop heavy workloads and inspect cooling immediately.",
                15,
            )
        elif temperature >= 80:
            finding(
                "warning",
                "Elevated thermal reading",
                f"{label} reported {temperature:.1f} °C.",
                "Inspect airflow and cooling before long gaming sessions.",
                7,
            )
    if battery_percent is not None and battery_percent < 20 and not battery_plugged:
        finding(
            "warning",
            "Low battery reserve",
            f"Battery is at {battery_percent:.0f}%.",
            "Connect AC power before launching a demanding game.",
            4,
        )
    if not power_items:
        power_items.append(
            {
                "label": "POWER / THERMAL",
                "value": "No battery or thermal sensors exposed",
                "status": "info",
            }
        )

    security_items = []
    defender = windows.get("Defender") if isinstance(windows.get("Defender"), dict) else {}
    defender_available = defender and any(
        defender.get(field) is not None
        for field in ("AntivirusEnabled", "RealTimeProtectionEnabled", "SignaturesOutOfDate")
    )
    if defender_available:
        antivirus = bool(defender.get("AntivirusEnabled"))
        realtime = bool(defender.get("RealTimeProtectionEnabled"))
        signatures_old = bool(defender.get("SignaturesOutOfDate"))
        security_items.extend(
            [
                {
                    "label": "MICROSOFT DEFENDER",
                    "value": "ENABLED" if antivirus else "DISABLED",
                    "status": "success" if antivirus else "critical",
                },
                {
                    "label": "REAL-TIME PROTECTION",
                    "value": "ENABLED" if realtime else "DISABLED",
                    "status": "success" if realtime else "critical",
                },
                {
                    "label": "SIGNATURE STATUS",
                    "value": "OUT OF DATE" if signatures_old else "CURRENT",
                    "status": "warning" if signatures_old else "success",
                },
            ]
        )
        if not antivirus or not realtime:
            finding(
                "critical",
                "Real-time malware protection disabled",
                "Microsoft Defender reported inactive protection.",
                "Enable an antivirus provider and real-time protection.",
                15,
            )
        elif signatures_old:
            finding(
                "warning",
                "Antivirus signatures out of date",
                "Defender signatures require an update.",
                "Run Windows Update or update Defender signatures.",
                5,
            )
    firewall_records = _as_record_list(windows.get("Firewall"))
    disabled_firewalls = []
    for firewall in firewall_records:
        enabled = bool(firewall.get("Enabled"))
        name = str(firewall.get("Name") or "Profile")
        security_items.append(
            {
                "label": f"FIREWALL {name.upper()}",
                "value": "ENABLED" if enabled else "DISABLED",
                "status": "success" if enabled else "warning",
            }
        )
        if not enabled:
            disabled_firewalls.append(name)
    if disabled_firewalls:
        finding(
            "warning",
            "Firewall profile disabled",
            ", ".join(disabled_firewalls) + " firewall profile(s) are disabled.",
            "Enable the profile unless another managed firewall replaces it.",
            5,
        )
    tpm = windows.get("TPM") if isinstance(windows.get("TPM"), dict) else {}
    if tpm:
        security_items.append(
            {
                "label": "TPM",
                "value": "READY"
                if tpm.get("Ready")
                else ("PRESENT" if tpm.get("Present") else "NOT PRESENT"),
                "status": "success" if tpm.get("Ready") else "info",
            }
        )
    device_errors = _as_record_list(windows.get("DeviceErrors"))
    security_items.append(
        {
            "label": "DEVICE ERRORS",
            "value": str(len(device_errors)),
            "status": "warning" if device_errors else "success",
        }
    )
    if device_errors:
        names = ", ".join(str(entry.get("Name") or "Unknown device") for entry in device_errors[:5])
        finding(
            "warning",
            "Windows device errors detected",
            names,
            "Open Device Manager and inspect devices showing warning symbols.",
            10,
        )
    if not security_items:
        security_items.append(
            {
                "label": "SECURITY TELEMETRY",
                "value": "Detailed Windows security telemetry unavailable",
                "status": "info",
            }
        )
    sections.append(
        {
            "title": "07  POWER / THERMAL",
            "status": "warning"
            if any(item["status"] in ("warning", "critical") for item in power_items)
            else "success",
            "items": power_items,
        }
    )
    sections.append(
        {
            "title": "08  SECURITY PERIMETER",
            "status": "warning"
            if any(item["status"] in ("warning", "critical") for item in security_items)
            else "success",
            "items": security_items,
        }
    )

    _system_report_progress(
        progress_callback,
        94,
        "LAUNCH ENVIRONMENT",
        "Recording launcher runtime and local capabilities",
    )
    environment_items = [
        {"label": "PYTHON", "value": python_version, "status": "success"},
        {"label": "XVVIIX DATA", "value": BASE_DIR, "status": "info"},
        {
            "label": "PROCESS MONITOR",
            "value": "ONLINE" if HAS_PSUTIL else "UNAVAILABLE",
            "status": "success" if HAS_PSUTIL else "warning",
        },
        {
            "label": "DATA VAULT",
            "value": "UNLOCKED / AES-256-GCM",
            "status": "success" if vault.vault_key is not None else "warning",
        },
        {"label": "ADMINISTRATOR", "value": "YES" if is_admin() else "NO", "status": "info"},
    ]
    sections.append(
        {"title": "09  XVVIIX ENVIRONMENT", "status": "success", "items": environment_items}
    )

    if not findings:
        findings.append(
            {
                "severity": "success",
                "title": "All monitored systems nominal",
                "detail": "No critical condition was detected in the available local telemetry.",
                "action": "Retain this report as a healthy baseline.",
            }
        )
    score = max(0, min(100, score))
    severity = "critical" if score < 60 else ("warning" if score < 85 else "success")
    duration_ms = int((time.perf_counter() - started) * 1000)
    summary = {
        "device": f"{manufacturer} {model}".strip(),
        "os": os_label,
        "cpu": cpu_model,
        "gpu": ", ".join(gpu_names[:3]) or "Detailed GPU inventory unavailable",
        "memory": format_bytes(memory_total),
        "storage": f"{format_bytes(storage_free)} free / {format_bytes(storage_total)}",
        "network": f"{len(active_interfaces)} active interface(s)",
        "uptime": format_time(uptime_seconds),
    }
    _system_report_progress(progress_callback, 100, "COMPLETE", f"Health score {score}/100")
    return {
        "id": uuid.uuid4().hex,
        "kind": "system_report",
        "timestamp": record_timestamp(epoch),
        "epoch": epoch,
        "title": f"SYS REPORT · {hostname}",
        "item_name": hostname,
        "item_path": "",
        "pid": 0,
        "exit_code": None,
        "exit_hex": "",
        "cause": f"SYSTEM HEALTH {score}/100",
        "severity": severity,
        "runtime_seconds": 0,
        "details": f"Comprehensive local diagnostic completed in {duration_ms / 1000:.1f} seconds.",
        "source": "xvviix_sys_report_v1",
        "health_score": score,
        "scan_duration_ms": duration_ms,
        "summary": summary,
        "sections": sections,
        "findings": findings,
        "suggestions": [entry.get("action", "") for entry in findings if entry.get("action")][:8],
    }


# ==========================================
# ANIMATIONS
# ==========================================


def widget_exists(widget):
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def cancel_animation(widget, channel):
    key = (id(widget), channel)
    animation_id = _anims.pop(key, None)
    _anim_tokens.pop(key, None)
    if animation_id is not None and root is not None:
        try:
            root.after_cancel(animation_id)
        except (tk.TclError, ValueError):
            pass


def start_animation(widget, channel, duration, steps, updater, easing=ease_out_cubic):
    """Run one cancellable animation per widget/channel."""
    global _animation_serial
    if root is None or not widget_exists(widget):
        return
    key = (id(widget), channel)
    cancel_animation(widget, channel)
    _animation_serial += 1
    token = _animation_serial
    _anim_tokens[key] = token
    frame_delay = max(8, int(duration / max(1, steps)))

    def tick(step=1):
        if _anim_tokens.get(key) != token or not widget_exists(widget):
            return
        progress = min(1.0, step / max(1, steps))
        try:
            updater(easing(progress))
        except (tk.TclError, ValueError, TypeError):
            _anims.pop(key, None)
            _anim_tokens.pop(key, None)
            return
        if step < steps:
            _anims[key] = root.after(frame_delay, tick, step + 1)
        else:
            _anims.pop(key, None)
            _anim_tokens.pop(key, None)

    _anims[key] = root.after(0, tick)


def resolve_widget_color(widget, color, fallback="#ffffff"):
    value = str(color)
    try:
        hex_to_rgb(value)
        return value
    except (TypeError, ValueError):
        try:
            red, green, blue = widget.winfo_rgb(value)
            return rgb_to_hex(red // 256, green // 256, blue // 256)
        except (tk.TclError, TypeError, ValueError):
            return fallback


def widget_color(widget, prop, fallback):
    try:
        return resolve_widget_color(widget, widget.cget(prop), fallback)
    except tk.TclError:
        return resolve_widget_color(widget, fallback)


def animate_widget_color(widget, prop, target, duration=150, steps=9, fallback=None):
    if not widget_exists(widget):
        return
    target = resolve_widget_color(widget, target, fallback or "#ffffff")
    start = widget_color(widget, prop, resolve_widget_color(widget, fallback or target))
    if start.lower() == target.lower():
        try:
            widget.configure(**{prop: target})
        except tk.TclError:
            pass
        return

    def update(progress):
        widget.configure(**{prop: lerp_color(start, target, progress)})

    start_animation(widget, f"color:{prop}", duration, steps, update)


def animate_numeric(widget, channel, start, target, setter, duration=150, steps=9):
    difference = target - start
    start_animation(
        widget,
        channel,
        duration,
        steps,
        lambda progress: setter(start + difference * progress),
        easing=ease_in_out_cubic,
    )


def current_grid_margin(widget, fallback):
    try:
        value = widget.grid_info().get("padx", fallback)
        if isinstance(value, (tuple, list)):
            value = value[0]
        return int(float(value))
    except (tk.TclError, TypeError, ValueError):
        return fallback


def animate_grid_margin(widget, target, duration=150):
    start = current_grid_margin(widget, target)
    animate_numeric(
        widget,
        "grid-margin",
        start,
        target,
        lambda value: widget.grid_configure(padx=int(round(value)), pady=int(round(value))),
        duration=duration,
        steps=8,
    )


def pointer_is_inside(widget):
    if not widget_exists(widget):
        return False
    try:
        pointed = root.winfo_containing(root.winfo_pointerx(), root.winfo_pointery())
        while pointed is not None:
            if pointed == widget:
                return True
            pointed = pointed.master
    except (tk.TclError, AttributeError):
        return False
    return False


def bind_animated_button(button, idle_bg, hover_bg, idle_fg=None, hover_fg=None, sound=False):
    """Attach interruption-safe hover, press, and release transitions."""
    button._anim_idle_bg = idle_bg
    button._anim_hover_bg = hover_bg
    button._anim_idle_fg = idle_fg
    button._anim_hover_fg = hover_fg

    def enter(_event=None):
        if str(button.cget("state")) == "disabled":
            return
        animate_widget_color(button, "bg", button._anim_hover_bg, 130, 8, idle_bg)
        if button._anim_hover_fg:
            animate_widget_color(button, "fg", button._anim_hover_fg, 130, 8, idle_fg)
        if sound:
            play_sound("hover")

    def leave(_event=None):
        animate_widget_color(button, "bg", button._anim_idle_bg, 180, 10, hover_bg)
        if button._anim_idle_fg:
            animate_widget_color(button, "fg", button._anim_idle_fg, 180, 10, hover_fg)

    def press(_event=None):
        if str(button.cget("state")) == "disabled":
            return
        pressed = lerp_color(button._anim_hover_bg, "#000000", 0.20)
        animate_widget_color(button, "bg", pressed, 70, 5, button._anim_hover_bg)
        play_sound("click")

    def release(_event=None):
        target = button._anim_hover_bg if pointer_is_inside(button) else button._anim_idle_bg
        animate_widget_color(button, "bg", target, 110, 7, button._anim_idle_bg)

    button.bind("<Enter>", enter)
    button.bind("<Leave>", leave)
    button.bind("<ButtonPress-1>", press, add="+")
    button.bind("<ButtonRelease-1>", release, add="+")
    return button


def animate_tab_state(button, selected):
    button._anim_idle_bg = TAB_ACT if selected else TAB_IN
    button._anim_hover_bg = ACCENT2 if selected else CARD2
    button._anim_idle_fg = TEXT if selected else SUBTEXT
    button._anim_hover_fg = TEXT
    animate_widget_color(button, "bg", button._anim_idle_bg, 180, 10, TAB_IN)
    animate_widget_color(button, "fg", button._anim_idle_fg, 180, 10, SUBTEXT)


def animate_entry_focus(entry, focused):
    animate_widget_color(
        entry,
        "highlightbackground",
        NEON if focused else BORDER,
        duration=160,
        steps=9,
        fallback=BORDER,
    )
    animate_widget_color(
        entry,
        "bg",
        lerp_color(CARD2, ACCENT, 0.10) if focused else CARD2,
        duration=160,
        steps=9,
        fallback=CARD2,
    )


def animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, hovered):
    if not widget_exists(card):
        return
    card._card_hovered = hovered
    surface_target = lerp_color(CARD, accent_color, 0.10) if hovered else CARD
    border_target = (
        NEON if getattr(card, "_library_selected", False) else (accent_color if hovered else BORDER)
    )
    stripe_target = lerp_color(accent_color, "#ffffff", 0.30) if hovered else accent_color
    card.configure(highlightthickness=1)
    animate_widget_color(card, "bg", surface_target, 170 if hovered else 220, 10, CARD)
    animate_widget_color(
        card, "highlightbackground", border_target, 170 if hovered else 220, 10, BORDER
    )
    animate_widget_color(stripe, "bg", stripe_target, 150 if hovered else 210, 9, accent_color)
    for surface in surface_widgets:
        animate_widget_color(surface, "bg", surface_target, 170 if hovered else 220, 10, CARD)
    # Keep margins fixed; hover must not resize the scrollable document.


def bind_card_animation(card, stripe, accent_color, surface_widgets, base_margin):
    card._card_hovered = False

    def enter(_event=None):
        leave_job = _card_leave_jobs.pop(id(card), None)
        if leave_job is not None:
            try:
                root.after_cancel(leave_job)
            except tk.TclError:
                pass
        if not getattr(card, "_card_hovered", False):
            animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, True)

    def delayed_leave():
        _card_leave_jobs.pop(id(card), None)
        if widget_exists(card) and not pointer_is_inside(card):
            animate_card_state(card, stripe, accent_color, surface_widgets, base_margin, False)

    def leave(_event=None):
        old_job = _card_leave_jobs.pop(id(card), None)
        if old_job is not None:
            try:
                root.after_cancel(old_job)
            except tk.TclError:
                pass
        _card_leave_jobs[id(card)] = root.after(35, delayed_leave)

    def bind_tree(widget):
        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")
        for child in widget.winfo_children():
            bind_tree(child)

    bind_tree(card)


def animate_card_entrance(card, stripe, accent_color, delay):
    try:
        card.configure(highlightbackground=BG2)
        stripe.configure(bg=CARD2)
    except tk.TclError:
        return

    def begin():
        _anims.pop((id(card), "entrance"), None)
        if not widget_exists(card):
            return
        hovered = getattr(card, "_card_hovered", False)
        border_target = (
            NEON
            if getattr(card, "_library_selected", False)
            else (accent_color if hovered else BORDER)
        )
        stripe_target = lerp_color(accent_color, "#ffffff", 0.30) if hovered else accent_color
        animate_widget_color(card, "highlightbackground", border_target, 220, 10, BG2)
        animate_widget_color(stripe, "bg", stripe_target, 240, 11, CARD2)

    key = (id(card), "entrance")
    _anims[key] = root.after(delay, begin)


def pulse_widget(widget, base_color, pulse_color, cycles=2, duration=180):
    sequence = []
    for _ in range(cycles):
        sequence.extend((pulse_color, base_color))

    key = (id(widget), "pulse-sequence")

    def run(index=0):
        if index >= len(sequence) or not widget_exists(widget):
            _anims.pop(key, None)
            return
        animate_widget_color(widget, "bg", sequence[index], duration, 9, base_color)
        _anims[key] = root.after(duration, run, index + 1)

    run()


def fade_window(window, start, target, duration=220, on_complete=None):
    try:
        window.attributes("-alpha", start)
    except tk.TclError:
        if on_complete:
            on_complete()
        return

    def update(value):
        window.attributes("-alpha", value)

    def finish_later():
        if widget_exists(window) and on_complete:
            on_complete()

    animate_numeric(window, "window-alpha", start, target, update, duration, 12)
    if on_complete:
        root.after(duration + 20, finish_later)


def cancel_widget_tree_animations(parent):
    widget_ids = set()

    def collect(widget):
        widget_ids.add(id(widget))
        try:
            for child in widget.winfo_children():
                collect(child)
        except tk.TclError:
            pass

    collect(parent)
    for key in [key for key in _anims if key[0] in widget_ids]:
        animation_id = _anims.pop(key, None)
        _anim_tokens.pop(key, None)
        if animation_id is not None:
            try:
                root.after_cancel(animation_id)
            except (tk.TclError, ValueError):
                pass
    for card_id in [card_id for card_id in _card_leave_jobs if card_id in widget_ids]:
        leave_job = _card_leave_jobs.pop(card_id, None)
        if leave_job is not None:
            try:
                root.after_cancel(leave_job)
            except (tk.TclError, ValueError):
                pass


def cancel_all_animations():
    for animation_id in list(_anims.values()):
        try:
            root.after_cancel(animation_id)
        except (tk.TclError, ValueError):
            pass
    for leave_job in list(_card_leave_jobs.values()):
        try:
            root.after_cancel(leave_job)
        except (tk.TclError, ValueError):
            pass
    _anims.clear()
    _anim_tokens.clear()
    _card_leave_jobs.clear()


# ==========================================
# UI FUNCTIONS
# ==========================================
def get_icon(icon_filename, size=48):
    if not HAS_PIL or not icon_filename:
        return None
    size = max(24, int(round(size / 4) * 4))
    key = f"{icon_filename}-{size}"
    if key in icon_cache:
        return icon_cache[key]
    icon_path = os.path.join(ICONS_DIR, icon_filename)
    image = None
    try:
        image = icons.load_display_icon(icon_path, size)
        if image is None:
            return None
        tk_image = ImageTk.PhotoImage(image, master=root)
        icon_cache[key] = tk_image
        while len(icon_cache) > 256:
            icon_cache.pop(next(iter(icon_cache)))
        return tk_image
    except (OSError, ValueError, TypeError, IndexError, tk.TclError):
        return None
    finally:
        if image is not None:
            image.close()


def resolve_card_art_source(item):
    """Return a user artwork path, or fall back to the launcher's cached icon."""
    artwork = str(item.get("artwork", "") or "").strip()
    if artwork:
        artwork = os.path.expanduser(os.path.expandvars(artwork))
        if not os.path.isabs(artwork):
            artwork = os.path.join(BASE_DIR, artwork)
        if os.path.isfile(artwork):
            return artwork, True
    icon_filename = str(item.get("icon", "") or "").strip()
    if icon_filename:
        icon_path = os.path.join(ICONS_DIR, icon_filename)
        if os.path.isfile(icon_path):
            return icon_path, False
    return "", False


def compose_card_backdrop(source_path, width, height, accent_color, is_artwork=False):
    return artwork.compose_card_backdrop(
        source_path,
        width,
        height,
        accent_color,
        is_artwork,
        card_color=CARD,
    )


def _card_backdrop_spec(item, width, height):
    if not HAS_PIL or not launcher_settings.get("card_art_enabled", True):
        return None
    source_path, is_artwork = resolve_card_art_source(item)
    if not source_path:
        return None
    width = max(220, min(720, int(round(width / 8) * 8)))
    height = max(88, min(220, int(round(height / 4) * 4)))
    try:
        stat = os.stat(source_path)
    except OSError:
        return None
    accent = str(item.get("color", ACCENT))
    key = (source_path, stat.st_mtime_ns, stat.st_size, width, height, accent, is_artwork, CARD)
    return key, (source_path, width, height, accent, is_artwork)


def _card_art_ready(epoch, key, image):
    callbacks = card_art_waiters.pop(key, []) if epoch == card_art_epoch else []
    if epoch != card_art_epoch or card_art_worker is None or not widget_exists(root):
        if image is not None:
            image.close()
        return
    if image is None:
        card_art_cache[key] = None
    else:
        try:
            photo = ImageTk.PhotoImage(image, master=root)
            card_art_cache[key] = photo
        except (RuntimeError, tk.TclError):
            return
        finally:
            image.close()
    while len(card_art_cache) > CARD_ART_CACHE_LIMIT:
        card_art_cache.pop(next(iter(card_art_cache)))
    photo = card_art_cache.get(key)
    if photo is not None:
        for callback in callbacks:
            callback(photo)


def get_card_backdrop(item, width, height, on_ready=None, *, spec=None):
    """Return a warm image immediately; prepare a cold image off the Tk thread."""
    global card_art_worker
    spec = spec if spec is not None else _card_backdrop_spec(item, width, height)
    if spec is None or not widget_exists(root):
        return None
    key, args = spec
    if key in card_art_cache:
        # Bounded LRU: frequently visible artwork is not evicted by old pages.
        image = card_art_cache.pop(key)
        card_art_cache[key] = image
        return image
    if key in card_art_waiters:
        if on_ready is not None:
            card_art_waiters[key].append(on_ready)
        return None
    if card_art_worker is None:
        epoch = card_art_epoch
        card_art_worker = artwork.ArtworkWorker(
            artwork.compose_card_backdrop,
            lambda cache_key, image: post_ui(_card_art_ready, epoch, cache_key, image),
            capacity=PAGE_SIZE + 4,
            logger=LOG,
        )
    card_art_waiters[key] = [on_ready] if on_ready is not None else []
    if not card_art_worker.submit(key, *args, card_color=CARD):
        card_art_waiters.pop(key, None)
    return None


def cancel_pending_card_artwork():
    if card_art_worker is not None:
        for key in card_art_worker.cancel_pending():
            card_art_waiters.pop(key, None)
    # Keep in-flight keys deduplicated, but discard callbacks for the old page.
    for key in card_art_waiters:
        card_art_waiters[key] = []


def stop_card_artwork():
    global card_art_worker, card_art_epoch
    card_art_epoch += 1
    if card_art_worker is not None:
        card_art_worker.stop()
        card_art_worker = None
    card_art_waiters.clear()
    card_art_cache.clear()


def set_card_art_enabled(enabled):
    """Persist the optional backdrop preference and redraw visible cards."""
    launcher_settings["card_art_enabled"] = bool(enabled and HAS_PIL)
    save_launcher_settings()
    if root is not None and widget_exists(root):
        refresh()


def toggle_card_art_enabled():
    set_card_art_enabled(not launcher_settings.get("card_art_enabled", True))


def _current_startup_command():
    return autostart.build_command(INSTALL_DIR)


def initialize_windows_startup():
    """Default on only after successful desktop initialization, never at import."""
    global startup_last_error
    if app_exit_event.is_set() or not data_loaded or not startup_service.supported:
        return
    try:
        state, added = startup_service.initialize_once(_current_startup_command())
        startup_last_error = ""
        if added:
            add_activity(
                "settings",
                "Start with Windows enabled",
                detail="Enabled for the current Windows account. Disable it in Settings at any time.",
                severity="success",
            )
        startup_checkpoint(
            "WINDOWS_STARTUP",
            "READY",
            "registered" if state.registered else "off; prior user choice respected",
        )
    except autostart.StartupError as exc:
        startup_last_error = str(exc)
        startup_checkpoint("WINDOWS_STARTUP", "DEGRADED", startup_last_error)


def set_windows_startup(enabled):
    global startup_last_error
    if app_exit_event.is_set():
        return False
    try:
        command = _current_startup_command() if enabled else None
        state = startup_service.set_enabled(bool(enabled), command)
        startup_last_error = ""
        if startup_menu_value is not None:
            startup_menu_value.set(state.registered)
        add_activity(
            "settings",
            "Start with Windows enabled" if state.registered else "Start with Windows disabled",
            detail="Current Windows account only; existing vault protection is unchanged.",
            severity="success",
        )
        return True
    except autostart.StartupError as exc:
        startup_last_error = str(exc)
        try:
            state = startup_service.state()
            if startup_menu_value is not None:
                startup_menu_value.set(state.registered)
        except autostart.StartupError:
            pass
        messagebox.showerror("Windows startup", startup_last_error, parent=root)
        return False


def _prepare_settings_menu():
    global settings_popup, startup_menu_value, startup_last_error
    if settings_popup is None or not widget_exists(settings_popup):
        settings_popup = tk.Menu(
            root,
            tearoff=0,
            bg=CARD2,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=TEXT,
            font=("Segoe UI", 10),
            bd=0,
        )
        startup_menu_value = DialogVariable(settings_popup, False, boolean=True)
    else:
        settings_popup.delete(0, "end")
    menu = settings_popup
    status = autostart.StartupState(supported=startup_service.supported)
    command = None
    read_error = ""
    if startup_service.supported:
        try:
            status = startup_service.state()
        except autostart.StartupError as exc:
            read_error = str(exc)
            startup_last_error = read_error
        if status.registered and not read_error:
            try:
                command = _current_startup_command()
                status = startup_service.state(command)
            except autostart.StartupError as exc:
                # An invalid/moved current path must not prevent disabling an old entry.
                startup_last_error = str(exc)
    startup_menu_value.set(status.registered)
    menu.add_checkbutton(
        label="Start with Windows" if status.supported else "Start with Windows (Windows only)",
        variable=startup_menu_value,
        command=lambda: set_windows_startup(startup_menu_value.get()),
        state="normal" if status.supported and not read_error else "disabled",
    )
    if status.supported:
        menu.add_command(label="Current account · password is still required", state="disabled")
        if status.registered and command and not status.matches_current:
            menu.add_command(
                label="Use this copy at Windows startup", command=lambda: set_windows_startup(True)
            )
        if read_error or startup_last_error:
            menu.add_command(label="Startup settings need attention", state="disabled")
    menu.add_separator()
    enabled = bool(launcher_settings.get("card_art_enabled", True) and HAS_PIL)
    menu.add_command(
        label=f"{'✓' if enabled else '  '}  Card artwork previews",
        command=toggle_card_art_enabled,
        state="normal" if HAS_PIL else "disabled",
    )
    menu.add_command(label="◈  Choose background color…", command=pick_bg_color)
    menu.add_separator()
    menu.add_command(label="◆  Data vault unlocked for this session", state="disabled")
    return menu


def show_theme_menu(_event=None):
    """One reusable Settings menu, including a truthful per-user startup toggle."""
    menu = _prepare_settings_menu()
    try:
        menu.tk_popup(bg_btn.winfo_rootx(), bg_btn.winfo_rooty() + bg_btn.winfo_height() + 4)
    finally:
        try:
            menu.grab_release()
        except tk.TclError:
            pass


def pick_bg_color():
    global BG, BG2
    result = colorchooser.askcolor(color=BG, title="Choose Background Color")
    if result and result[1]:
        chosen = result[1]
        BG = chosen
        r, g, b = hex_to_rgb(chosen)
        BG2 = rgb_to_hex(min(255, r + 10), min(255, g + 10), min(255, b + 15))
        for widget in (root, canvas, frame, tab_bar):
            animate_widget_color(widget, "bg", BG, 320, 14, chosen)
        refresh()


def format_activity_age(epoch):
    return activity_age(epoch).upper()


def _activity_records():
    with data_lock:
        return [dict(entry) for entry in recent_activity if entry.get("kind") != "crash"]


def clear_recent_activity():
    if not _activity_records():
        return False
    if not messagebox.askyesno(
        "Clear activity history?",
        "Clear the saved recent-activity history?\n\nPrograms, libraries, playtime totals and system reports will not be removed.",
        parent=activity_history.window if activity_history is not None else root,
    ):
        return False
    with data_lock:
        previous = list(recent_activity)
        recent_activity.clear()
    if not vault._save(vault.ACTIVITY_FILE, recent_activity):
        with data_lock:
            recent_activity[:] = previous
        return False
    update_activity_rail()
    return True


def _activity_closed():
    global activity_history
    activity_history = None


def show_recent_activity(_event=None):
    global activity_history
    if app_exit_event.is_set():
        return
    if activity_history is not None and not activity_history.closed:
        activity_history.window.deiconify()
        activity_history.window.lift()
        activity_history.refresh(force=True)
        return
    activity_history = ActivityView(
        root, _activity_records, clear_recent_activity, on_close=_activity_closed
    )


def _activity_clock_tick():
    global activity_clock_job
    activity_clock_job = None
    if app_exit_event.is_set() or not widget_exists(root):
        return
    if root.winfo_viewable():
        update_activity_rail()
    activity_clock_job = root.after(30000, _activity_clock_tick)


def update_activity_rail():
    global _activity_rail_signature
    if not widget_exists(activity_primary_lbl) or not widget_exists(activity_secondary_lbl):
        return
    entries = _activity_records()[:2]
    if not entries:
        primary_text, secondary_text, color = (
            "NO RECENT ACTIVITY",
            "Open history to inspect launches and library changes",
            SUBTEXT,
        )
    else:
        primary = entries[0]
        colors = {
            "critical": RED,
            "warning": ORANGE,
            "success": GREEN_HOVER,
            "error": RED,
            "info": NEON,
        }
        primary_text = f"{primary['title']}   ·   {format_activity_age(primary.get('epoch'))}"
        color = colors.get(primary.get("severity", "info"), NEON)
        if len(entries) > 1:
            secondary = entries[1]
            secondary_text = (
                f"{secondary['title']}   ·   {format_activity_age(secondary.get('epoch'))}"
            )
        else:
            secondary_text = primary.get("detail") or "Click to open activity history"
        if len(primary_text) > 58:
            primary_text = primary_text[:57].rstrip() + "…"
        if len(secondary_text) > 64:
            secondary_text = secondary_text[:63].rstrip() + "…"
    signature = (primary_text, secondary_text, color)
    if signature != _activity_rail_signature:
        activity_primary_lbl.configure(text=primary_text, fg=color)
        activity_secondary_lbl.configure(text=secondary_text, fg=SUBTEXT)
        _activity_rail_signature = signature
    if activity_history is not None and not activity_history.closed:
        activity_history.refresh()


def update_scale(event=None):
    global current_scale, resize_job
    if event is not None and event.widget is not root:
        return
    width = root.winfo_width()
    new_scale = max(0.7, min(width / 950, 1.8))
    if abs(new_scale - current_scale) <= 0.05:
        return
    current_scale = new_scale
    if resize_job is not None:
        try:
            root.after_cancel(resize_job)
        except tk.TclError:
            pass
    resize_job = root.after(120, apply_resized_layout)


def apply_resized_layout():
    global resize_job
    resize_job = None
    apply_topbar_scaling()
    refresh()


def apply_topbar_scaling():
    width = root.winfo_width()
    font_size = 9 if width < 900 else (10 if width < 1350 else 11)
    sort_labels = {"pinned": "PINNED FIRST", "name": "NAME  A–Z", "playtime": "MOST PLAYED"}
    if width < 900:
        search_width, name_width = 8, 7
        scan_btn.config(text="⌁  SCAN", padx=8)
        add_btn.config(text="＋  ADD", padx=8)
        sort_btn.config(text="⇅  SORT", padx=8)
    elif width < 1350:
        search_width, name_width = 18, 15
        scan_btn.config(text="⌁  SCAN SYSTEM", padx=15)
        add_btn.config(text="＋  ADD ENTRY", padx=18)
        sort_btn.config(text=f"⇅  {sort_labels[current_sort]}", padx=15)
    else:
        search_width, name_width = 22, 19
        scan_btn.config(text="⌁  SCAN SYSTEM", padx=18)
        add_btn.config(text="＋  ADD ENTRY", padx=18)
        sort_btn.config(text=f"⇅  {sort_labels[current_sort]}", padx=15)
    search_entry.config(font=("Segoe UI", font_size), width=search_width)
    name_entry.config(font=("Segoe UI", font_size), width=name_width)
    scan_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))
    add_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))
    sort_btn.config(font=("Segoe UI", max(8, font_size - 1), "bold"))


def current_view_items():
    if active_tab in ("reports", "monitor"):
        return []
    query = search_var.get().strip().casefold()
    with data_lock:
        items = list(current_list())
    if query:
        items = [item for item in items if query in item["name"].casefold()]
    return sort_items(items)


def search_games(*_):
    global search_job
    page_by_tab[active_tab] = 0
    if search_job is not None:
        try:
            root.after_cancel(search_job)
        except tk.TclError:
            pass
    search_job = root.after(120, apply_search)


def apply_search():
    global search_job
    search_job = None
    if library_view is not None:
        library_view.clear_selection()
    canvas.yview_moveto(0)
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def refresh(reset_page=False):
    if reset_page:
        page_by_tab[active_tab] = 0
    update_stats()
    update_activity_rail()
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def update_stats():
    with data_lock:
        total_g = len(games)
        total_a = len(apps)
        total_time = sum(item["playtime"] for item in games + apps)
    stats_lbl.config(
        text=f"{total_g} GAMES   ·   {total_a} APPS   ·   {format_time(total_time)} TOTAL ACTIVITY"
    )
    compact_tabs = root.winfo_width() < 920
    if compact_tabs:
        games_tab_btn.config(text=f"GAMES {total_g}", padx=10)
        apps_tab_btn.config(text=f"APPS {total_a}", padx=10)
        founded_tab_btn.config(text=f"FOUND {len(founded)}", padx=10)
        reports_tab_btn.config(text=f"REPORTS {len(system_report_items())}", padx=10)
        monitor_tab_btn.config(text="MONITOR", padx=10)
    else:
        games_tab_btn.config(text=f"◉  GAMES   {total_g}", padx=22)
        apps_tab_btn.config(text=f"◇  WORKSPACE   {total_a}", padx=22)
        founded_tab_btn.config(text=f"✦  DISCOVERED   {len(founded)}", padx=18)
        reports_tab_btn.config(text=f"△  REPORTS   {len(system_report_items())}", padx=18)
        monitor_tab_btn.config(text="⌁  MONITOR", padx=18)
    if header_canvas is not None and header_stats_id is not None:
        try:
            header_canvas.itemconfigure(
                header_stats_id,
                text=f"{total_g:02d} GAMES     {total_a:02d} APPS     {len(system_report_items()):02d} REPORTS     {format_time(total_time).upper()} ACTIVE",
            )
        except tk.TclError:
            pass


def change_page(delta):
    page_by_tab[active_tab] = max(0, page_by_tab[active_tab] + delta)
    canvas.yview_moveto(0)
    if active_tab == "reports":
        display_reports()
    elif active_tab == "monitor":
        display_hardware_monitor()
    else:
        display_items(current_view_items())


def change_sort():
    global current_sort
    sorts = ["pinned", "name", "playtime"]
    idx = sorts.index(current_sort)
    current_sort = sorts[(idx + 1) % len(sorts)]
    page_by_tab[active_tab] = 0
    apply_topbar_scaling()
    refresh()


def set_library_controls_visible(visible):
    if visible:
        if not drop_container.winfo_manager():
            drop_container.pack(fill="x", pady=(0, 10), before=container)
        if not topbar.winfo_manager():
            topbar.pack(fill="x", padx=30, pady=(0, 14), before=container)
    else:
        drop_container.pack_forget()
        topbar.pack_forget()


def switch_tab(tab):
    global active_tab, search_job
    if tab not in page_by_tab:
        return
    active_tab = tab
    page_by_tab[tab] = 0
    animate_tab_state(games_tab_btn, tab == "games")
    animate_tab_state(apps_tab_btn, tab == "apps")
    animate_tab_state(founded_tab_btn, tab == "founded")
    animate_tab_state(reports_tab_btn, tab == "reports")
    animate_tab_state(monitor_tab_btn, tab == "monitor")
    set_library_controls_visible(tab not in ("reports", "monitor"))
    if tab == "monitor":
        request_hardware_monitor_start()
    else:
        cancel_hardware_monitor_view_refresh()
        schedule_hardware_monitor_idle_stop()
    if search_var.get():
        search_var.set("")
        if search_job is not None:
            try:
                root.after_cancel(search_job)
            except tk.TclError:
                pass
            search_job = None
    canvas.yview_moveto(0)
    refresh()


def show_context_menu(event, item):
    menu = tk.Menu(
        root,
        tearoff=0,
        bg=CARD2,
        fg=TEXT,
        activebackground=ACCENT,
        activeforeground="white",
        font=("Segoe UI", 10),
    )
    menu.add_command(label="✏️ Rename", command=lambda: rename_item(item))
    menu.add_command(label="📁 Open Location", command=lambda: open_file_location(item))
    menu.add_command(label="↻ Change Location", command=lambda: change_item_location(item))
    menu.add_command(
        label="＋ Add Icon",
        command=lambda: add_custom_icon(item),
        state="normal" if HAS_PIL else "disabled",
    )
    if active_tab in ("games", "apps"):
        menu.add_command(
            label="■ End Task",
            command=lambda: end_task(item),
            state="normal" if is_item_running(item) else "disabled",
        )
    menu.add_separator()
    mv_txt = "Move to Apps" if active_tab == "games" else "Move to Games"
    menu.add_command(label=f"➡️ {mv_txt}", command=lambda: move_item(item))
    menu.add_command(label="❌ Delete", command=lambda: delete_item(item), foreground=RED)
    menu.tk_popup(event.x_root, event.y_root)


def make_founded_action_btn(parent, item):
    btn_row = tk.Frame(parent, bg=CARD)
    btn_row.pack(fill="x")

    def make_btn(text, bg_idle, fg_idle, bg_hover, fg_hover, cmd):
        b = tk.Button(
            btn_row,
            text=text,
            bg=bg_idle,
            fg=fg_idle,
            relief="flat",
            font=("Segoe UI", 7, "bold"),
            cursor="hand2",
            pady=4,
            bd=0,
            command=cmd,
        )
        return bind_animated_button(b, bg_idle, bg_hover, fg_idle, fg_hover)

    make_btn("＋  GAMES", CARD2, SUBTEXT, ACCENT, TEXT, lambda: move_to_games(item)).pack(
        side="left", expand=True, fill="x", padx=(0, 3)
    )
    make_btn("＋  WORKSPACE", CARD2, SUBTEXT, GREEN, TEXT, lambda: move_to_apps(item)).pack(
        side="left", expand=True, fill="x", padx=(3, 0)
    )
    return btn_row


def make_action_btn(parent, text, bg_idle, fg_idle, bg_hover, fg_hover, cmd, scale=1.0):
    b = tk.Button(
        parent,
        text=text,
        bg=bg_idle,
        fg=fg_idle,
        relief="flat",
        font=("Segoe UI", max(7, int(7 * scale)), "bold"),
        cursor="hand2",
        pady=max(4, int(4 * scale)),
        bd=0,
        command=cmd,
    )
    return bind_animated_button(b, bg_idle, bg_hover, fg_idle, fg_hover)


def compact_display_path(path, limit=46):
    clean = clean_path(path)
    parent = ntpath.basename(ntpath.dirname(clean))
    filename = ntpath.basename(clean)
    value = f"{parent}  /  {filename}" if parent else filename
    if len(value) <= limit:
        return value
    left = max(10, limit // 2 - 2)
    right = max(10, limit - left - 3)
    return f"{value[:left]}...{value[-right:]}"


def bind_context_tree(widget, item):
    widget.bind("<Button-3>", lambda event, entry=item: show_context_menu(event, entry), add="+")
    for child in widget.winfo_children():
        bind_context_tree(child, item)


def _scroll_library(*args):
    if library_view is not None:
        library_view._cancel_restore()
    canvas.yview(*args)


def _wheel_library(event):
    if library_view is not None:
        library_view._cancel_restore()
    canvas.yview_scroll(int(-event.delta / 120), "units")


def clear_item_widgets():
    global library_view
    if library_view is not None:
        library_view.dispose()
        library_view = None
    cancel_pending_card_artwork()
    cancel_widget_tree_animations(frame)
    for widget in frame.winfo_children():
        widget.destroy()


def sys_report_window_exists():
    try:
        return sys_report_window is not None and bool(sys_report_window.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def request_system_report_cancel():
    if not sys_report_running:
        return
    sys_report_cancel.set()
    if widget_exists(sys_report_status_lbl):
        sys_report_status_lbl.config(text="CANCELLING DIAGNOSTIC...", fg=ORANGE)


def create_system_report_window():
    global sys_report_window, sys_report_status_lbl, sys_report_detail_lbl, sys_report_progress
    sys_report_window = tk.Toplevel(root)
    sys_report_window.title("XVVIIX SYS REPORT")
    sys_report_window.geometry("720x490")
    sys_report_window.minsize(640, 450)
    sys_report_window.configure(bg=BG)
    sys_report_window.transient(root)
    sys_report_window.grab_set()
    sys_report_window.protocol("WM_DELETE_WINDOW", request_system_report_cancel)
    enable_win11_round_corners(sys_report_window)
    tk.Frame(sys_report_window, bg=CYAN, height=4).pack(fill="x")
    body = tk.Frame(sys_report_window, bg=BG, padx=34, pady=26)
    body.pack(fill="both", expand=True)
    tk.Label(
        body,
        text="△  SYS REPORT  //  FULL SPECTRUM DIAGNOSTIC",
        bg=BG,
        fg=TEXT,
        font=("Segoe UI Black", 16, "bold"),
    ).pack(anchor="w")
    tk.Label(
        body,
        text="LOCAL HARDWARE · OS · STORAGE · NETWORK · SECURITY TELEMETRY",
        bg=BG,
        fg=NEON,
        font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(5, 22))
    sys_report_status_lbl = tk.Label(
        body,
        text="INITIALIZING",
        bg=BG,
        fg=CYAN,
        font=("Consolas", 12, "bold"),
        anchor="w",
    )
    sys_report_status_lbl.pack(fill="x")
    sys_report_detail_lbl = tk.Label(
        body,
        text="Preparing diagnostic pipeline",
        bg=BG,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        anchor="w",
    )
    sys_report_detail_lbl.pack(fill="x", pady=(5, 14))
    sys_report_progress = tk.Canvas(
        body,
        height=18,
        bg=CARD2,
        highlightthickness=1,
        highlightbackground=BORDER,
        bd=0,
    )
    sys_report_progress.pack(fill="x")
    sys_report_progress.fill_id = sys_report_progress.create_rectangle(
        0, 0, 0, 18, fill=CYAN, outline=""
    )
    sys_report_progress.text_id = sys_report_progress.create_text(
        10,
        9,
        text="0%",
        anchor="w",
        fill=TEXT,
        font=("Consolas", 7, "bold"),
    )
    phases = tk.Frame(
        body, bg=CARD, padx=14, pady=12, highlightbackground=BORDER_SOFT, highlightthickness=1
    )
    phases.pack(fill="both", expand=True, pady=(18, 16))
    phase_text = (
        "01  COMMAND PLATFORM        04  GRAPHICS SUBSYSTEM\n"
        "02  COMPUTE CORE            05  STORAGE MODULES\n"
        "03  MEMORY ARRAY            06  NETWORK LINKS\n"
        "07  POWER / THERMAL         08  SECURITY PERIMETER\n"
        "09  XVVIIX ENVIRONMENT       10  HEALTH ANALYSIS"
    )
    tk.Label(
        phases,
        text=phase_text,
        bg=CARD,
        fg=SUBTEXT,
        font=("Consolas", 9, "bold"),
        justify="left",
    ).pack(anchor="w")
    cancel_button = tk.Button(
        body,
        text="CANCEL REPORT",
        bg=CARD2,
        fg=SUBTEXT,
        relief="flat",
        bd=0,
        padx=18,
        pady=8,
        cursor="hand2",
        font=("Segoe UI", 8, "bold"),
        command=request_system_report_cancel,
    )
    cancel_button.pack(anchor="e")
    bind_animated_button(cancel_button, CARD2, RED, SUBTEXT, TEXT)
    fade_window(sys_report_window, 0.0, 1.0, 220)


def update_system_report_progress(percent, phase, detail=""):
    if not sys_report_window_exists():
        return
    if widget_exists(sys_report_status_lbl):
        sys_report_status_lbl.config(text=str(phase).upper(), fg=CYAN)
    if widget_exists(sys_report_detail_lbl):
        sys_report_detail_lbl.config(text=str(detail)[:120])
    if widget_exists(sys_report_progress):
        sys_report_progress.update_idletasks()
        width = max(1, sys_report_progress.winfo_width())
        fill_width = int(width * max(0, min(100, int(percent))) / 100)
        sys_report_progress.coords(sys_report_progress.fill_id, 0, 0, fill_width, 18)
        sys_report_progress.itemconfigure(sys_report_progress.text_id, text=f"{int(percent):02d}%")


def close_system_report_window():
    global sys_report_window
    if not sys_report_window_exists():
        sys_report_window = None
        return
    try:
        sys_report_window.grab_release()
        sys_report_window.destroy()
    except tk.TclError:
        pass
    sys_report_window = None


def finish_system_report(report):
    global sys_report_running, selected_report_id
    sys_report_running = False
    close_system_report_window()
    if report is None:
        messagebox.showerror(
            "SYS REPORT", "The report could not be stored in the encrypted report library."
        )
        return
    selected_report_id = report.get("id")
    add_activity(
        "system_report",
        f"SYS REPORT complete · {report.get('health_score', 0)}/100",
        detail=report.get("details", "Local diagnostic complete"),
        severity=report.get("severity", "info"),
    )
    switch_tab("reports")


def fail_system_report(message, cancelled=False):
    global sys_report_running
    sys_report_running = False
    close_system_report_window()
    if not cancelled:
        messagebox.showerror("SYS REPORT", f"The diagnostic could not be completed:\n\n{message}")


def start_system_report():
    global sys_report_running
    if sys_report_running:
        if sys_report_window_exists():
            sys_report_window.deiconify()
            sys_report_window.lift()
        return
    if vault.vault_key is None:
        messagebox.showerror("SYS REPORT", "Unlock the XVVIIX data vault before creating a report.")
        return
    sys_report_running = True
    sys_report_cancel.clear()
    create_system_report_window()

    def progress(percent, phase, detail):
        post_ui(update_system_report_progress, percent, phase, detail)

    def worker():
        try:
            raw_report = collect_system_report(progress)
            if sys_report_cancel.is_set():
                raise SystemReportCancelled()
            stored = add_report(raw_report)
            post_ui(finish_system_report, stored)
        except SystemReportCancelled:
            post_ui(fail_system_report, "Cancelled", True)
        except Exception as exc:
            LOG.exception("SYS REPORT failed")
            post_ui(fail_system_report, str(exc), False)

    threading.Thread(target=worker, daemon=True, name="sys-report-scanner").start()


def report_severity_color(severity):
    return {
        "critical": RED,
        "error": RED,
        "warning": ORANGE,
        "info": CYAN,
        "success": GREEN,
    }.get(str(severity).lower(), CYAN)


def format_report_timestamp(report):
    timestamp = str(report.get("timestamp", "") or "")
    if "T" in timestamp:
        timestamp = timestamp.replace("T", " ", 1)
    if "+" in timestamp:
        timestamp = timestamp.rsplit("+", 1)[0]
    return timestamp[:19] or "TIME UNAVAILABLE"


def format_report_text(report):
    if report.get("kind") != "system_report":
        raise ValueError("Only system diagnostic reports are supported")
    lines = [
        "XVVIIX SYS REPORT",
        f"Report ID: {report.get('id', '')}",
        f"Time: {report.get('timestamp', '')}",
        f"Device: {report.get('item_name', '')}",
        f"Health score: {report.get('health_score', 0)}/100",
        f"Scan duration: {report.get('scan_duration_ms', 0) / 1000:.1f}s",
        "",
        "SUMMARY",
    ]
    for label, value in report.get("summary", {}).items():
        lines.append(f"{label.upper()}: {value}")
    lines.extend(("", "FINDINGS"))
    for finding in report.get("findings", []):
        lines.append(f"[{finding.get('severity', 'info').upper()}] {finding.get('title', '')}")
        if finding.get("detail"):
            lines.append(f"  {finding['detail']}")
        if finding.get("action"):
            lines.append(f"  Action: {finding['action']}")
    for section in report.get("sections", []):
        lines.extend(("", section.get("title", "SECTION")))
        for item in section.get("items", []):
            lines.append(f"{item.get('label', '')}: {item.get('value', '')}")
    return "\n".join(lines) + "\n"


def copy_report(report):
    if report.get("kind") != "system_report":
        return
    try:
        root.clipboard_clear()
        root.clipboard_append(format_report_text(report))
        root.update_idletasks()
        add_activity(
            "report",
            f"Report copied · {report.get('item_name', 'System')}",
            detail=report.get("cause", "System diagnostic"),
            severity="info",
        )
    except tk.TclError as exc:
        messagebox.showerror("Copy Report", f"Could not copy the report:\n{exc}")


def delete_report(report):
    global selected_report_id
    if report.get("kind") != "system_report":
        return False
    if not messagebox.askyesno("Delete System Report", "Delete this saved system diagnostic?"):
        return False
    report_id = report.get("id")
    with data_lock:
        previous = list(reports)
        reports[:] = [
            entry
            for entry in reports
            if entry.get("id") != report_id or entry.get("kind") != "system_report"
        ]
    if not vault._save(vault.REPORTS_FILE, reports):
        with data_lock:
            reports[:] = previous
        return False
    if selected_report_id == report_id:
        selected_report_id = None
    refresh(reset_page=True)
    return True


def clear_all_reports():
    global selected_report_id
    if not system_report_items():
        return False
    if not messagebox.askyesno(
        "Clear System Reports",
        "Delete every saved system diagnostic?\n\nThis cannot be undone.",
        icon="warning",
    ):
        return False
    with data_lock:
        previous = list(reports)
        reports[:] = [entry for entry in reports if entry.get("kind") != "system_report"]
    if not vault._save(vault.REPORTS_FILE, reports):
        with data_lock:
            reports[:] = previous
        return False
    selected_report_id = None
    refresh(reset_page=True)
    return True


def view_report(report):
    global selected_report_id
    if report.get("kind") != "system_report":
        return
    selected_report_id = report.get("id")
    canvas.yview_moveto(0)
    display_reports()


def show_previous_reports():
    global selected_report_id
    selected_report_id = None
    canvas.yview_moveto(0)
    display_reports()


def display_system_report_detail(report):
    clear_item_widgets()
    frame.grid_columnconfigure(0, weight=1)
    frame.grid_columnconfigure(1, weight=1)
    compact = root.winfo_width() < 980
    score = int(report.get("health_score", 0))
    severity_color = report_severity_color(report.get("severity"))

    header = tk.Frame(
        frame,
        bg=CARD,
        padx=22,
        pady=18,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 10))
    heading = tk.Frame(header, bg=CARD)
    heading.pack(side="left", fill="both", expand=True)
    tk.Label(
        heading,
        text="△  XVVIIX SYS REPORT  //  MISSION SYSTEMS",
        bg=CARD,
        fg=TEXT,
        font=("Segoe UI Black", 15 if not compact else 12, "bold"),
    ).pack(anchor="w")
    tk.Label(
        heading,
        text=f"REPORT {report.get('id', '')[:12].upper()}   ·   {format_report_timestamp(report)}   ·   {report.get('item_name', '').upper()}",
        bg=CARD,
        fg=NEON,
        font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(5, 0))
    score_panel = tk.Frame(header, bg=CARD2, padx=18, pady=8)
    score_panel.pack(side="right", padx=(16, 0))
    tk.Label(
        score_panel, text=f"{score:02d}", bg=CARD2, fg=severity_color, font=("Consolas", 24, "bold")
    ).pack()
    tk.Label(
        score_panel, text="HEALTH / 100", bg=CARD2, fg=SUBTEXT, font=("Consolas", 7, "bold")
    ).pack()

    actions = tk.Frame(frame, bg=BG)
    actions.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    make_action_btn(
        actions,
        "←  PREVIOUS REPORTS",
        CARD2,
        SUBTEXT,
        ACCENT,
        TEXT,
        show_previous_reports,
    ).pack(side="left")
    make_action_btn(
        actions,
        "COPY FULL REPORT",
        CARD2,
        SUBTEXT,
        CYAN,
        TEXT,
        lambda: copy_report(report),
    ).pack(side="right", padx=(6, 0))
    make_action_btn(
        actions,
        "DELETE",
        CARD2,
        MUTED,
        RED,
        TEXT,
        lambda: delete_report(report),
    ).pack(side="right")

    summary = tk.Frame(
        frame,
        bg=BG2,
        padx=14,
        pady=12,
        highlightbackground=BORDER_SOFT,
        highlightthickness=1,
    )
    summary.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    summary_values = list(report.get("summary", {}).items())
    for index, (label, value) in enumerate(summary_values[:8]):
        cell = tk.Frame(summary, bg=CARD, padx=10, pady=8)
        cell.grid(
            row=index // (2 if compact else 4),
            column=index % (2 if compact else 4),
            sticky="nsew",
            padx=3,
            pady=3,
        )
        tk.Label(cell, text=label.upper(), bg=CARD, fg=MUTED, font=("Consolas", 7, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            cell,
            text=value,
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 8, "bold"),
            wraplength=220 if compact else 190,
            justify="left",
        ).pack(anchor="w", pady=(3, 0))
    for column in range(2 if compact else 4):
        summary.grid_columnconfigure(column, weight=1, uniform="sys-summary")

    findings = report.get("findings", [])
    finding_panel = tk.Frame(
        frame,
        bg=CARD,
        padx=16,
        pady=12,
        highlightbackground=BORDER_SOFT,
        highlightthickness=1,
    )
    finding_panel.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
    tk.Label(
        finding_panel,
        text=f"HEALTH ANALYSIS  //  {len(findings):02d} FINDINGS",
        bg=CARD,
        fg=NEON,
        font=("Consolas", 9, "bold"),
    ).pack(anchor="w", pady=(0, 7))
    for finding in findings[:12]:
        color = report_severity_color(finding.get("severity"))
        row = tk.Frame(finding_panel, bg=CARD2, padx=9, pady=7)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="●", bg=CARD2, fg=color, font=("Segoe UI", 8, "bold")).pack(
            side="left", padx=(0, 7)
        )
        text = finding.get("title", "")
        if finding.get("detail"):
            text += f"  —  {finding['detail']}"
        if finding.get("action"):
            text += f"  //  ACTION: {finding['action']}"
        tk.Label(
            row,
            text=text,
            bg=CARD2,
            fg="#c4d1e5",
            font=("Segoe UI", 8),
            wraplength=max(450, root.winfo_width() - 160),
            justify="left",
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

    sections = report.get("sections", [])
    section_start = 4
    columns = 1 if compact else 2
    for index, section in enumerate(sections):
        section_color = report_severity_color(section.get("status"))
        panel = tk.Frame(
            frame,
            bg=CARD,
            padx=14,
            pady=12,
            highlightbackground=BORDER_SOFT,
            highlightthickness=1,
        )
        panel.grid(
            row=section_start + index // columns,
            column=index % columns,
            columnspan=2 if compact else 1,
            sticky="nsew",
            padx=10 if compact else (10, 5) if index % 2 == 0 else (5, 10),
            pady=6,
        )
        top = tk.Frame(panel, bg=CARD)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(
            top, text=section.get("title", "SYSTEM"), bg=CARD, fg=TEXT, font=("Consolas", 9, "bold")
        ).pack(side="left")
        tk.Label(top, text="●", bg=CARD, fg=section_color, font=("Segoe UI", 9, "bold")).pack(
            side="right"
        )
        for item in section.get("items", []):
            row = tk.Frame(panel, bg=CARD2, padx=8, pady=5)
            row.pack(fill="x", pady=1)
            tk.Label(
                row,
                text=item.get("label", ""),
                bg=CARD2,
                fg=MUTED,
                font=("Consolas", 7, "bold"),
                width=19,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=item.get("value", ""),
                bg=CARD2,
                fg=report_severity_color(item.get("status"))
                if item.get("status") in ("warning", "critical", "error")
                else "#c4d1e5",
                font=("Segoe UI", 8),
                anchor="w",
                justify="left",
                wraplength=360 if compact else 300,
            ).pack(side="left", fill="x", expand=True)


def _monitor_percent(value):
    if monitor_clamp_percent is None:
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError, OverflowError):
            return None
    return monitor_clamp_percent(value)


def _monitor_bytes(value):
    return monitor_format_bytes(value) if monitor_format_bytes is not None else "--"


def _monitor_rate(value):
    return monitor_format_rate(value) if monitor_format_rate is not None else "--"


def _monitor_uptime(seconds):
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "--"
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def cancel_hardware_monitor_idle_stop():
    global hardware_monitor_idle_job
    if hardware_monitor_idle_job is not None and root is not None:
        try:
            root.after_cancel(hardware_monitor_idle_job)
        except (tk.TclError, ValueError):
            pass
    hardware_monitor_idle_job = None


def _finish_hardware_monitor_idle_stop():
    global hardware_monitor_state
    hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
    if _monitor_tab_visible() or _hardware_overlay_visible() or monitor_overlay_requested:
        request_hardware_monitor_start()


def _stop_hardware_monitor_if_idle():
    global hardware_monitor, hardware_monitor_state, hardware_monitor_idle_job
    hardware_monitor_idle_job = None
    if _monitor_tab_visible() or _hardware_overlay_visible():
        return
    service = hardware_monitor
    hardware_monitor = None
    if service is None:
        hardware_monitor_state = "standby" if HAS_HARDWARE_MONITOR else "unavailable"
        return
    hardware_monitor_state = "stopping"

    def worker():
        service.stop()
        post_ui(_finish_hardware_monitor_idle_stop)

    threading.Thread(target=worker, daemon=True, name="hardware-monitor-stopper").start()


def schedule_hardware_monitor_idle_stop():
    global hardware_monitor_idle_job
    cancel_hardware_monitor_idle_stop()
    if root is not None and hardware_monitor is not None:
        hardware_monitor_idle_job = root.after(6000, _stop_hardware_monitor_if_idle)


def _finish_hardware_monitor_start(service, error=""):
    global hardware_monitor, hardware_monitor_state, hardware_monitor_error
    global monitor_overlay_requested
    if monitor_stop.is_set() or root is None or not widget_exists(root):
        if service is not None:
            service.stop()
        return
    if error:
        hardware_monitor = None
        hardware_monitor_state = "failed"
        hardware_monitor_error = str(error)[:240]
        LOG.warning("Integrated Hardware Monitor unavailable: %s", error)
    else:
        hardware_monitor = service
        hardware_monitor_state = "ready"
        hardware_monitor_error = ""
    if _monitor_tab_visible():
        display_hardware_monitor()
    if monitor_overlay_requested:
        monitor_overlay_requested = False
        if hardware_monitor is not None:
            open_hardware_overlay()
    if not _monitor_tab_visible() and not _hardware_overlay_visible():
        schedule_hardware_monitor_idle_stop()


def request_hardware_monitor_start():
    """Initialize expensive GPU/process telemetry off the Tk thread, on demand."""
    global hardware_monitor_state, hardware_monitor_error
    cancel_hardware_monitor_idle_stop()
    if hardware_monitor is not None and hardware_monitor.is_running():
        hardware_monitor_state = "ready"
        return True
    if hardware_monitor_state == "starting":
        return False
    if hardware_monitor_state == "stopping":
        if root is not None:
            root.after(120, request_hardware_monitor_start)
        return False
    if not HAS_HARDWARE_MONITOR:
        hardware_monitor_state = "unavailable"
        hardware_monitor_error = "psutil is not installed"
        return False
    hardware_monitor_state = "starting"
    hardware_monitor_error = ""

    def worker():
        service = None
        error = ""
        try:
            service = HardwareMonitorService(interval=1.0)
            service.start()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if service is not None:
                service.stop()
            service = None
        post_ui(_finish_hardware_monitor_start, service, error)

    threading.Thread(target=worker, daemon=True, name="hardware-monitor-loader").start()
    return False


def _monitor_metric_card(parent, column, title, accent):
    card = tk.Frame(
        parent,
        bg=CARD,
        padx=15,
        pady=13,
        highlightbackground=BORDER_SOFT,
        highlightthickness=1,
    )
    card.grid(row=0, column=column, sticky="nsew", padx=5, pady=5)
    tk.Label(
        card,
        text=title,
        bg=CARD,
        fg=accent,
        font=("Consolas", 8, "bold"),
        anchor="w",
    ).pack(fill="x")
    value = tk.Label(
        card,
        text="--",
        bg=CARD,
        fg=TEXT,
        font=("Segoe UI Black", 22, "bold"),
        anchor="w",
    )
    value.pack(fill="x", pady=(3, 1))
    detail = tk.Label(
        card,
        text="Waiting for telemetry",
        bg=CARD,
        fg=SUBTEXT,
        font=("Segoe UI", 8),
        anchor="w",
        justify="left",
        wraplength=300 if root.winfo_width() >= 980 else 280,
    )
    detail.pack(fill="x")
    chart = tk.Canvas(card, height=38, bg=CARD2, highlightthickness=0, bd=0)
    chart.pack(fill="x", pady=(10, 0))
    return {"card": card, "value": value, "detail": detail, "chart": chart, "accent": accent}


def _draw_monitor_history(chart, values, color):
    if not widget_exists(chart):
        return
    width = chart.winfo_width()
    height = chart.winfo_height()
    width = max(40, width if width > 1 else chart.winfo_pixels(chart.cget("width")))
    height = max(20, height if height > 1 else chart.winfo_pixels(chart.cget("height")))
    items = getattr(chart, "_monitor_history_items", None)
    if items is None:
        items = (
            chart.create_line(0, 0, 1, 0, fill=BORDER, width=1),
            chart.create_line(0, 0, 1, 0, fill=BORDER_SOFT, width=1),
            chart.create_line(0, 0, 1, 0, fill=color, width=2, smooth=True),
        )
        chart._monitor_history_items = items
    chart.coords(items[0], 0, height - 1, width, height - 1)
    chart.coords(items[1], 0, height // 2, width, height // 2)
    samples = list(values)
    if not samples:
        chart.itemconfigure(items[2], state="hidden")
        return
    if len(samples) == 1:
        samples.insert(0, samples[0])
    step = width / (len(samples) - 1)
    points = []
    for index, raw in enumerate(samples):
        value = _monitor_percent(raw) or 0.0
        points.extend((index * step, height - value / 100 * (height - 3) - 1))
    chart.coords(items[2], *points)
    chart.itemconfigure(items[2], state="normal", fill=color)


def cancel_hardware_monitor_view_refresh():
    global monitor_refresh_job
    if monitor_refresh_job is not None and root is not None:
        try:
            root.after_cancel(monitor_refresh_job)
        except (tk.TclError, ValueError):
            pass
    monitor_refresh_job = None


def _schedule_hardware_monitor_view_refresh():
    global monitor_refresh_job
    cancel_hardware_monitor_view_refresh()
    if root is not None and _monitor_tab_visible():
        monitor_refresh_job = root.after(900, update_hardware_monitor_view)


def display_hardware_monitor():
    cancel_hardware_monitor_view_refresh()
    clear_item_widgets()
    monitor_ui.clear()
    for column in range(4):
        frame.grid_columnconfigure(column, weight=1 if column == 0 else 0, uniform="")

    header = tk.Frame(
        frame,
        bg=CARD,
        padx=22,
        pady=16,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 6))
    compact_header = root.winfo_width() < 900
    heading = tk.Frame(header, bg=CARD)
    if compact_header:
        heading.pack(fill="x")
    else:
        heading.pack(side="left", fill="both", expand=True)
    tk.Label(
        heading,
        text="⌁  HARDWARE MONITOR  //  LIVE TELEMETRY",
        bg=CARD,
        fg=TEXT,
        font=("Segoe UI Black", 16, "bold"),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        heading,
        text="NORMALIZED 0–100% METRICS  ·  CURRENT-USER PROCESS INVENTORY  ·  LOCAL-ONLY SAMPLING",
        bg=CARD,
        fg=NEON,
        font=("Consolas", 8, "bold"),
        anchor="w",
    ).pack(fill="x", pady=(5, 0))
    controls = tk.Frame(header, bg=CARD)
    if compact_header:
        controls.pack(fill="x", pady=(10, 0))
    else:
        controls.pack(side="right", padx=(16, 0))
    status_label = tk.Label(
        controls,
        text="INITIALIZING",
        bg=CARD2,
        fg=ORANGE,
        font=("Consolas", 8, "bold"),
        padx=12,
        pady=7,
    )
    status_label.pack(side="left", padx=(0, 7))
    overlay_button = make_action_btn(
        controls,
        "OPEN OVERLAY",
        GREEN,
        TEXT,
        GREEN_HOVER,
        TEXT,
        open_hardware_overlay,
        scale=1.0,
    )
    overlay_button.pack(side="left")
    monitor_ui["header"] = header
    monitor_ui["status"] = status_label
    monitor_ui["overlay_button"] = overlay_button

    if hardware_monitor is None:
        waiting = hardware_monitor_state in {"standby", "starting", "stopping"}
        unavailable = tk.Frame(frame, bg=BG)
        unavailable.grid(row=1, column=0, sticky="ew", pady=90)
        tk.Label(
            unavailable,
            text="⌁",
            bg=BG,
            fg=CYAN if waiting else ORANGE,
            font=("Segoe UI", 46, "bold"),
        ).pack()
        tk.Label(
            unavailable,
            text="STARTING HARDWARE TELEMETRY" if waiting else "HARDWARE TELEMETRY UNAVAILABLE",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Black", 15, "bold"),
        ).pack(pady=(3, 6))
        detail = (
            "Loading monitoring services in the background. The launcher remains responsive."
            if waiting
            else (
                hardware_monitor_error
                or "Install psutil and restart XVVIIX to activate the integrated monitor."
            )
        )
        tk.Label(
            unavailable,
            text=detail,
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
            wraplength=620,
            justify="center",
        ).pack()
        status_label.config(text="STARTING" if waiting else "OFFLINE", fg=CYAN if waiting else RED)
        if waiting and hardware_monitor_state == "standby":
            request_hardware_monitor_start()
        return

    metric_panel = tk.Frame(frame, bg=BG)
    metric_panel.grid(row=1, column=0, sticky="ew", padx=5, pady=0)
    metric_columns = 2 if root.winfo_width() < 980 else 4
    for column in range(metric_columns):
        metric_panel.grid_columnconfigure(column, weight=1, uniform="monitor-metric")
    metric_specs = (
        ("cpu", "CPU LOAD", CYAN),
        ("gpu", "GPU LOAD", ACCENT2),
        ("memory", "SYSTEM RAM", GREEN_HOVER),
        ("storage", "SYSTEM STORAGE", ORANGE),
    )
    for index, (key, title, accent) in enumerate(metric_specs):
        card = _monitor_metric_card(metric_panel, index % metric_columns, title, accent)
        card["card"].grid_configure(row=index // metric_columns)
        monitor_ui[key] = card

    secondary = tk.Frame(frame, bg=BG)
    secondary.grid(row=2, column=0, sticky="ew", padx=5, pady=0)
    secondary_columns = 1 if root.winfo_width() < 900 else 2
    for column in range(secondary_columns):
        secondary.grid_columnconfigure(column, weight=1, uniform="monitor-secondary")
    for index, (key, title, accent) in enumerate(
        (
            ("network_panel", "NETWORK LINK", NEON),
            ("system_panel", "SYSTEM IDENTITY", ACCENT2),
        )
    ):
        panel = tk.Frame(
            secondary,
            bg=CARD,
            padx=16,
            pady=13,
            highlightbackground=BORDER_SOFT,
            highlightthickness=1,
        )
        panel.grid(
            row=index // secondary_columns,
            column=index % secondary_columns,
            sticky="nsew",
            padx=5,
            pady=5,
        )
        tk.Label(panel, text=title, bg=CARD, fg=accent, font=("Consolas", 8, "bold")).pack(
            anchor="w"
        )
        primary = tk.Label(
            panel,
            text="Waiting for telemetry",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        )
        primary.pack(fill="x", pady=(7, 2))
        secondary_label = tk.Label(
            panel,
            text="--",
            bg=CARD,
            fg=SUBTEXT,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
        )
        secondary_label.pack(fill="x")
        tertiary_label = tk.Label(
            panel,
            text="--",
            bg=CARD,
            fg=MUTED,
            font=("Consolas", 7),
            anchor="w",
            justify="left",
            wraplength=max(300, (root.winfo_width() - 110) // secondary_columns),
        )
        tertiary_label.pack(fill="x", pady=(4, 0))
        monitor_ui[key] = {
            "primary": primary,
            "secondary": secondary_label,
            "tertiary": tertiary_label,
        }

    top_panels = TopProcessPanels(frame)
    top_panels.container.grid(row=3, column=0, sticky="ew", padx=10, pady=(6, 12))
    monitor_ui["top_panels"] = top_panels
    monitor_ui["last_snapshot_timestamp"] = -1.0
    update_hardware_monitor_view(force=True)


def update_hardware_monitor_view(force=False):
    global monitor_refresh_job, monitor_history_timestamp
    monitor_refresh_job = None
    if not _monitor_tab_visible() or hardware_monitor is None or not monitor_ui:
        return
    try:
        snapshot = hardware_monitor.snapshot()
        timestamp = float(snapshot.get("timestamp", 0.0) or 0.0)
        if not force and timestamp == monitor_ui.get("last_snapshot_timestamp"):
            _schedule_hardware_monitor_view_refresh()
            return
        monitor_ui["last_snapshot_timestamp"] = timestamp
        status = str(snapshot.get("status") or "starting")
        status_label = monitor_ui.get("status")
        if widget_exists(status_label):
            status_label.config(
                text="LIVE" if status == "online" else status.upper(),
                fg=GREEN_HOVER if status == "online" else ORANGE,
            )

        cpu = snapshot.get("cpu", {})
        gpu = snapshot.get("gpu", {})
        memory = snapshot.get("memory", {})
        storage = snapshot.get("storage", {})
        network = snapshot.get("network", {})
        system = snapshot.get("system", {})
        static = snapshot.get("static", {})
        cpu_pct = _monitor_percent(cpu.get("percent")) or 0.0
        gpu_pct = _monitor_percent(gpu.get("usage"))
        memory_pct = _monitor_percent(memory.get("percent")) or 0.0
        storage_pct = _monitor_percent(storage.get("percent")) or 0.0
        if timestamp > monitor_history_timestamp:
            monitor_history["cpu"].append(cpu_pct)
            monitor_history["gpu"].append(gpu_pct or 0.0)
            monitor_history["memory"].append(memory_pct)
            monitor_history["storage"].append(storage_pct)
            monitor_history_timestamp = timestamp

        cpu_card = monitor_ui["cpu"]
        cpu_card["value"].config(text=f"{cpu_pct:.0f}%")
        frequency = cpu.get("current_mhz")
        temperature = cpu.get("temperature")
        cpu_card["detail"].config(
            text=(
                f"{static.get('physical_cores', '--')}C / {static.get('logical_cores', '--')}T"
                f"   ·   {frequency / 1000:.2f} GHz"
                if frequency
                else f"{static.get('physical_cores', '--')}C / {static.get('logical_cores', '--')}T   ·   CLOCK --"
            )
            + (f"   ·   {temperature:.0f}°C" if temperature is not None else "")
        )

        gpu_card = monitor_ui["gpu"]
        gpu_card["value"].config(text=f"{gpu_pct:.0f}%" if gpu_pct is not None else "N/A")
        gpu_detail = str(gpu.get("name") or "GPU telemetry unavailable")
        if gpu.get("vram_used") is not None:
            gpu_detail += f"   ·   {_monitor_bytes(gpu.get('vram_used'))} / {_monitor_bytes(gpu.get('vram_total'))}"
        if gpu.get("temperature") is not None:
            gpu_detail += f"   ·   {gpu['temperature']:.0f}°C"
        gpu_extended = []
        if gpu.get("clock_mhz") is not None:
            gpu_extended.append(f"CLOCK {gpu['clock_mhz']:.0f} MHz")
        if gpu.get("power_w") is not None:
            gpu_extended.append(f"POWER {gpu['power_w']:.0f} W")
        if gpu.get("fan_percent") is not None:
            gpu_extended.append(f"FAN {gpu['fan_percent']:.0f}%")
        if gpu_extended:
            gpu_detail += "\n" + "   ·   ".join(gpu_extended)
        gpu_card["detail"].config(text=gpu_detail[:180])

        memory_card = monitor_ui["memory"]
        memory_card["value"].config(text=f"{memory_pct:.0f}%")
        memory_card["detail"].config(
            text=(
                f"{_monitor_bytes(memory.get('used'))} USED   ·   {_monitor_bytes(memory.get('available'))} AVAILABLE"
                f"\nSWAP {_monitor_percent(memory.get('swap_percent')) or 0.0:.0f}%   ·   {_monitor_bytes(memory.get('swap_used'))} / {_monitor_bytes(memory.get('swap_total'))}"
            )
        )
        storage_card = monitor_ui["storage"]
        storage_card["value"].config(text=f"{storage_pct:.0f}%")
        storage_card["detail"].config(
            text=(
                f"{_monitor_bytes(storage.get('free'))} FREE / {_monitor_bytes(storage.get('total'))}"
                f"\nREAD {_monitor_rate(storage.get('read_rate'))}   ·   WRITE {_monitor_rate(storage.get('write_rate'))}"
            )
        )
        for key in ("cpu", "gpu", "memory", "storage"):
            _draw_monitor_history(
                monitor_ui[key]["chart"], monitor_history[key], monitor_ui[key]["accent"]
            )

        latency = network.get("latency_ms")
        interfaces = network.get("interfaces", [])
        network_panel = monitor_ui["network_panel"]
        network_panel["primary"].config(
            text=f"↓  {_monitor_rate(network.get('download_rate'))}     ↑  {_monitor_rate(network.get('upload_rate'))}     PING  {latency:.0f} ms"
            if latency is not None
            else f"↓  {_monitor_rate(network.get('download_rate'))}     ↑  {_monitor_rate(network.get('upload_rate'))}     PING  --"
        )
        interface_names = (
            ", ".join(str(item.get("name")) for item in interfaces[:6])
            or "No active interface details"
        )
        network_panel["secondary"].config(
            text=f"{len(interfaces)} ACTIVE LINK(S)   ·   {network.get('ipv4_count', 0)} IPv4 ADDRESS(ES)   ·   {interface_names}"
        )
        network_details = []
        for interface in interfaces[:4]:
            speed = (
                f"{interface.get('speed_mbps', 0)} Mbps"
                if interface.get("speed_mbps")
                else "speed unavailable"
            )
            addresses = ", ".join(interface.get("ipv4", [])[:2]) or "no IPv4"
            network_details.append(f"{interface.get('name', 'LINK')}: {speed}, {addresses}")
        network_panel["tertiary"].config(
            text="   ·   ".join(network_details)
            if network_details
            else "No local interface metadata exposed"
        )

        system_panel = monitor_ui["system_panel"]
        system_panel["primary"].config(
            text=f"{static.get('hostname', 'Unknown')}   ·   {static.get('os', 'Unknown OS')}   ·   UPTIME {_monitor_uptime(system.get('uptime_seconds'))}"
        )
        battery = system.get("battery")
        battery_text = "NO BATTERY"
        if battery and battery.get("percent") is not None:
            battery_text = (
                f"BATTERY {battery['percent']:.0f}% {'AC' if battery.get('plugged') else 'MOBILE'}"
            )
        system_panel["secondary"].config(
            text=f"{system.get('process_count', 0)} TOTAL PROCESSES   ·   {system.get('user_process_count', 0)} USER PROCESSES   ·   {battery_text}"
        )
        temperatures = system.get("temperatures", [])
        temperature_text = (
            ", ".join(
                f"{entry.get('sensor', 'SENSOR')} {entry.get('temperature', 0):.0f}°C"
                for entry in temperatures[:4]
            )
            or "No thermal sensors exposed"
        )
        system_panel["tertiary"].config(
            text=f"{static.get('cpu_model', 'Unknown CPU')}   ·   {static.get('architecture', 'Unknown architecture')}   ·   {temperature_text}"
        )
        process_signature = snapshot.get("process_revision", timestamp)
        if process_signature != monitor_ui.get("process_signature"):
            monitor_ui["top_panels"].update(
                snapshot.get("top_cpu", []),
                snapshot.get("top_memory", []),
                snapshot.get("user_process_total", 0),
            )
            monitor_ui["process_signature"] = process_signature
    except (tk.TclError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        LOG.debug("Hardware Monitor view refresh skipped: %s", exc)
    if active_tab == "monitor":
        _schedule_hardware_monitor_view_refresh()


def _monitor_tab_visible():
    return bool(active_tab == "monitor" and widget_exists(root) and root.winfo_viewable())


def _hardware_overlay_visible():
    view = monitor_overlay_ui.get("view")
    return bool(view is not None and not view.closed and view.visible)


def _overlay_telemetry():
    service = hardware_monitor
    if service is not None:
        return service.overlay_snapshot()
    return {
        "timestamp": 0.0,
        "status": "starting"
        if hardware_monitor_state in {"starting", "stopping", "standby"}
        else "unavailable",
    }


def _overlay_visibility_changed(visible):
    global monitor_overlay_requested
    monitor_overlay_requested = bool(visible)
    if visible:
        cancel_hardware_monitor_idle_stop()
        request_hardware_monitor_start()
    elif not _monitor_tab_visible():
        schedule_hardware_monitor_idle_stop()


def _main_monitor_visibility_changed(event):
    if event.widget is not root:
        return
    if _monitor_tab_visible():
        request_hardware_monitor_start()
        if hardware_monitor is not None:
            update_hardware_monitor_view(force=True)
    else:
        cancel_hardware_monitor_view_refresh()
        if not _hardware_overlay_visible():
            schedule_hardware_monitor_idle_stop()


def close_hardware_overlay():
    global monitor_overlay, monitor_overlay_job, monitor_overlay_requested
    monitor_overlay_requested = False
    view = monitor_overlay_ui.get("view")
    if view is not None:
        view.destroy()
    elif widget_exists(monitor_overlay):
        monitor_overlay.destroy()
    monitor_overlay = None
    monitor_overlay_job = None
    monitor_overlay_ui.clear()
    if not _monitor_tab_visible():
        schedule_hardware_monitor_idle_stop()


def toggle_hardware_overlay_mode():
    view = monitor_overlay_ui.get("view")
    if view is not None:
        view.toggle_compact()


def _save_overlay_opacity(value):
    launcher_settings["monitor_overlay_opacity"] = value
    save_launcher_settings()


def open_hardware_overlay():
    global monitor_overlay, monitor_overlay_requested
    if not HAS_HARDWARE_MONITOR:
        messagebox.showerror(
            "Hardware Monitor",
            "Hardware telemetry is unavailable. Install psutil and restart XVVIIX.",
        )
        return
    cancel_hardware_monitor_idle_stop()
    view = monitor_overlay_ui.get("view")
    if view is not None and not view.closed:
        view.window.deiconify()
        view.window.lift()
        view.poll_now()
        return
    # Show immediately: constructing/priming sensors stays on the existing worker.
    monitor_overlay_requested = True
    view = GameMonitorOverlay(
        root,
        _overlay_telemetry,
        opacity=launcher_settings.get(
            "monitor_overlay_opacity", GameMonitorOverlay.DEFAULT_OPACITY
        ),
        on_opacity=_save_overlay_opacity,
        on_close=close_hardware_overlay,
        on_visibility=_overlay_visibility_changed,
        logger=LOG,
        fps=60,
    )
    monitor_overlay = view.window
    monitor_overlay_ui["view"] = view
    request_hardware_monitor_start()


def update_hardware_overlay():
    view = monitor_overlay_ui.get("view")
    if view is not None:
        view.poll_now()


def display_reports():
    with data_lock:
        selected = next((entry for entry in reports if entry.get("id") == selected_report_id), None)
    if selected is not None and selected.get("kind") == "system_report":
        display_system_report_detail(selected)
        return
    clear_item_widgets()
    for column in range(4):
        frame.grid_columnconfigure(column, weight=1 if column == 0 else 0, uniform="")
    report_items = system_report_items()
    total = total_all = len(report_items)
    critical = sum((report.get("severity") in ("critical", "error") for report in report_items))
    command = tk.Frame(
        frame, bg=CARD, padx=20, pady=16, highlightbackground=BORDER, highlightthickness=1
    )
    command.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 10))
    compact_reports = root.winfo_width() < 900
    title_column = tk.Frame(command, bg=CARD)
    if compact_reports:
        title_column.pack(fill="x")
    else:
        title_column.pack(side="left", fill="x", expand=True)
    tk.Label(
        title_column,
        text="△  MISSION REPORTS  //  SYSTEMS ANALYSIS",
        bg=CARD,
        fg=TEXT,
        font=("Segoe UI Black", 12 if compact_reports else 15, "bold"),
    ).pack(anchor="w")
    tk.Label(
        title_column,
        text="SYSTEM DIAGNOSTICS  ·  ENCRYPTED LOCAL ARCHIVE  ·  NO CLOUD",
        bg=CARD,
        fg=NEON,
        font=("Consolas", 8, "bold"),
    ).pack(anchor="w", pady=(4, 0))
    metrics = tk.Frame(command, bg=CARD)
    if compact_reports:
        metrics.pack(fill="x", pady=(10, 0))
    else:
        metrics.pack(side="right")
    run_report_btn = tk.Button(
        metrics,
        text="△  RUN SYS REPORT",
        bg=GREEN,
        fg=TEXT,
        relief="flat",
        bd=0,
        padx=14,
        pady=7,
        cursor="hand2",
        font=("Segoe UI", 8, "bold"),
        command=start_system_report,
        state="disabled" if sys_report_running else "normal",
        disabledforeground=SUBTEXT,
    )
    run_report_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(run_report_btn, GREEN, GREEN_HOVER, TEXT, TEXT)
    tk.Label(
        metrics,
        text=f"{total_all:02d}  REPORTS",
        bg=CARD2,
        fg=NEON,
        font=("Consolas", 9, "bold"),
        padx=12,
        pady=7,
    ).pack(side="left", padx=4)
    tk.Label(
        metrics,
        text=f"{critical:02d}  CRITICAL",
        bg=CARD2,
        fg=RED if critical else GREEN,
        font=("Consolas", 9, "bold"),
        padx=12,
        pady=7,
    ).pack(side="left", padx=4)
    clear_btn = tk.Button(
        metrics,
        text="CLEAR ALL",
        bg=CARD2,
        fg=MUTED,
        relief="flat",
        bd=0,
        padx=12,
        pady=7,
        cursor="hand2",
        font=("Segoe UI", 8, "bold"),
        command=clear_all_reports,
        state="normal" if total_all else "disabled",
        disabledforeground=BORDER,
    )
    clear_btn.pack(side="left", padx=(8, 0))
    bind_animated_button(clear_btn, CARD2, RED, MUTED, TEXT)
    filters = tk.Frame(frame, bg=BG)
    filters.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
    tk.Label(filters, text="PREVIOUS REPORTS", bg=BG, fg=MUTED, font=("Consolas", 8, "bold")).pack(
        side="left", padx=(2, 12)
    )
    if not report_items:
        empty = tk.Frame(frame, bg=BG)
        empty.grid(row=2, column=0, pady=85)
        tk.Label(empty, text="◇", bg=BG, fg=GREEN, font=("Segoe UI", 46, "bold")).pack()
        tk.Label(
            empty,
            text="NO SAVED SYSTEM REPORTS",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Black", 16, "bold"),
        ).pack(pady=(4, 6))
        tk.Label(
            empty,
            text="Run SYS REPORT to save a system diagnostic.",
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
        ).pack()
        return
    total_pages = max(1, (total + REPORT_PAGE_SIZE - 1) // REPORT_PAGE_SIZE)
    page = min(page_by_tab["reports"], total_pages - 1)
    page_by_tab["reports"] = page
    page_items = report_items[page * REPORT_PAGE_SIZE : (page + 1) * REPORT_PAGE_SIZE]
    wrap = max(420, root.winfo_width() - 250)
    for index, report in enumerate(page_items, start=2):
        severity_color = report_severity_color(report.get("severity"))
        card = tk.Frame(
            frame, bg=CARD, padx=18, pady=14, highlightbackground=BORDER_SOFT, highlightthickness=1
        )
        card.grid(row=index, column=0, sticky="ew", padx=10, pady=6)
        tk.Frame(card, bg=severity_color, width=4).pack(side="left", fill="y", padx=(0, 14))
        body = tk.Frame(card, bg=CARD)
        body.pack(side="left", fill="both", expand=True)
        meta = tk.Frame(body, bg=CARD)
        meta.pack(fill="x")
        tk.Label(
            meta,
            text=f"  {report.get('severity', 'warning').upper()}  ",
            bg=severity_color,
            fg=TEXT,
            font=("Segoe UI", 7, "bold"),
            pady=2,
        ).pack(side="left")
        kind_label = report.get("kind", "system_report").replace("_", " ").upper()
        tk.Label(
            meta,
            text=f"{kind_label}   ·   {format_report_timestamp(report)}",
            bg=CARD,
            fg=MUTED,
            font=("Consolas", 8, "bold"),
        ).pack(side="left", padx=10)
        tk.Label(
            meta,
            text=f"HEALTH {report.get('health_score', 0):02d}/100",
            bg=CARD,
            fg=severity_color,
            font=("Consolas", 9, "bold"),
        ).pack(side="right")
        tk.Label(
            body,
            text=report.get("title"),
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(9, 2))
        tk.Label(
            body,
            text=report.get("cause", "Unknown failure"),
            bg=CARD,
            fg=severity_color,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            body,
            text=f"SCAN {report.get('scan_duration_ms', 0) / 1000:.1f}S   ·   {len(report.get('sections', [])):02d} SYSTEM MODULES   ·   SOURCE {report.get('source', '').upper()}",
            bg=CARD,
            fg=SUBTEXT,
            font=("Consolas", 8),
            anchor="w",
        ).pack(fill="x", pady=(5, 8))
        tk.Label(
            body,
            text=report.get("details", ""),
            bg=CARD2,
            fg="#c4d1e5",
            font=("Segoe UI", 9),
            justify="left",
            anchor="w",
            wraplength=wrap,
            padx=10,
            pady=8,
        ).pack(fill="x")
        suggestions = report.get("suggestions", [])
        if suggestions:
            tk.Label(
                body,
                text="HEALTH ACTIONS  //  " + "   •   ".join(suggestions[:3]),
                bg=CARD,
                fg=SUBTEXT,
                font=("Segoe UI", 8, "bold"),
                justify="left",
                anchor="w",
                wraplength=wrap,
            ).pack(fill="x", pady=(8, 2))
        actions = tk.Frame(card, bg=CARD)
        actions.pack(side="right", fill="y", padx=(14, 0))
        view_btn = make_action_btn(
            actions,
            "VIEW FULL",
            GREEN,
            TEXT,
            GREEN_HOVER,
            TEXT,
            lambda entry=report: view_report(entry),
            scale=1.0,
        )
        view_btn.pack(fill="x", pady=(0, 5))
        copy_btn = make_action_btn(
            actions,
            "COPY",
            CARD2,
            SUBTEXT,
            ACCENT,
            TEXT,
            lambda entry=report: copy_report(entry),
            scale=1.0,
        )
        copy_btn.pack(fill="x", pady=(0, 5))
        delete_btn = make_action_btn(
            actions,
            "DELETE",
            CARD2,
            MUTED,
            RED,
            TEXT,
            lambda entry=report: delete_report(entry),
            scale=1.0,
        )
        delete_btn.pack(fill="x")
    if total_pages > 1:
        navigation = tk.Frame(frame, bg=BG, pady=12)
        navigation.grid(row=len(page_items) + 2, column=0, sticky="ew")
        previous = tk.Button(
            navigation,
            text="←  PREVIOUS",
            command=lambda: change_page(-1),
            state="normal" if page > 0 else "disabled",
            bg=CARD2,
            fg=TEXT,
            disabledforeground=MUTED,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=18,
            pady=7,
        )
        previous.pack(side="left", padx=10)
        tk.Label(
            navigation,
            text=f"REPORT PAGE {page + 1:02d} / {total_pages:02d}",
            bg=BG,
            fg=SUBTEXT,
            font=("Consolas", 9, "bold"),
        ).pack(side="left", expand=True)
        following = tk.Button(
            navigation,
            text="NEXT  →",
            command=lambda: change_page(1),
            state="normal" if page + 1 < total_pages else "disabled",
            bg=CARD2,
            fg=TEXT,
            disabledforeground=MUTED,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=18,
            pady=7,
        )
        following.pack(side="right", padx=10)


def _build_library_card(item, i, layout, selected, animate, view):
    width, columns = layout["width"], layout["columns"]
    s = layout["scale"]
    icon_size = int(48 * s)
    base_pad = int(10 * s)
    extract_icon_in_background(item, item.get("path", ""))
    color = item["color"]
    color_lite = lerp_color(color, "#ffffff", 0.30)
    card_margin = int(8 * s)
    card = tk.Frame(
        frame,
        bg=CARD,
        padx=base_pad,
        pady=base_pad,
        highlightbackground=BORDER_SOFT,
        highlightthickness=1,
    )
    card.grid(
        row=1 + i // columns,
        column=i % columns,
        padx=card_margin,
        pady=card_margin,
        sticky="nsew",
    )
    surface_widgets = []
    trainer_btn = end_btn = None
    selection_var = DialogVariable(card, value=selected, boolean=True)

    def selection_box(parent):
        return tk.Checkbutton(
            parent,
            variable=selection_var,
            command=lambda: view.set_checked(item, selection_var.get()),
            bg=CARD,
            fg=NEON,
            activebackground=CARD,
            selectcolor=CARD2,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )

    stripe = tk.Frame(card, bg=color, height=3)
    stripe.pack(fill="x", pady=(0, 10))

    if active_tab == "games":
        category_text, category_color = "GAME", ACCENT
    elif active_tab == "apps":
        category_text, category_color = "APP", CYAN
    else:
        category_text, category_color = "DISCOVERED", ORANGE
    extension = (
        ntpath.splitext(clean_path(item.get("path", "")))[1].replace(".", "").upper() or "FILE"
    )

    content_width = canvas.winfo_width()
    if content_width < 300:
        content_width = max(300, width - 60)
    estimated_card_width = int(content_width / columns) - (card_margin * 2)
    art_width = max(220, estimated_card_width - (base_pad * 2))
    art_height = max(104, int(108 * s))
    art_spec = (
        _card_backdrop_spec(item, art_width, art_height)
        if active_tab in ("games", "apps")
        else None
    )

    if art_spec is not None:
        identity_panel = tk.Canvas(
            card,
            width=art_width,
            height=art_height,
            bg=CARD,
            bd=0,
            highlightthickness=0,
        )
        identity_panel.pack(fill="x", pady=(0, 10))
        background_item = identity_panel.create_image(0, 0, anchor="nw", state="hidden")
        panel_ref = weakref.ref(identity_panel)

        def apply_art(photo, ref=panel_ref, image_item=background_item):
            panel = ref()
            if widget_exists(panel):
                panel.itemconfigure(image_item, image=photo, state="normal")
                panel.backdrop_image = photo

        backdrop = get_card_backdrop(item, art_width, art_height, apply_art, spec=art_spec)
        if backdrop is not None:
            apply_art(backdrop)
        surface_widgets.append(identity_panel)

        tag_width = max(49, int((len(category_text) * 7 + 19) * s))
        identity_panel.create_rectangle(
            10,
            8,
            10 + tag_width,
            29,
            fill=category_color,
            outline="",
        )
        identity_panel.create_text(
            10 + tag_width / 2,
            18,
            text=category_text,
            fill=TEXT,
            font=("Segoe UI", max(7, int(7 * s)), "bold"),
        )
        identity_panel.create_window(
            art_width - 8, 18, window=selection_box(identity_panel), anchor="e"
        )
        identity_panel.create_text(
            art_width - 34,
            18,
            text="◆  PINNED" if item["pinned"] else extension,
            anchor="e",
            fill=ACCENT2 if item["pinned"] else MUTED,
            font=(
                "Segoe UI" if item["pinned"] else "Consolas",
                max(7, int(7 * s)),
                "bold",
            ),
        )

        icon = get_icon(item["icon"], icon_size)
        icon_center_y = int(69 * s)
        text_x = max(70, int(71 * s))
        if icon:
            identity_panel.create_image(
                12,
                icon_center_y,
                image=icon,
                anchor="w",
            )
            identity_panel.foreground_icon = icon
        else:
            letter = item["name"][0].upper() if item["name"] else "?"
            identity_panel.create_text(
                max(31, int(34 * s)),
                icon_center_y,
                text=letter,
                fill=color_lite,
                font=("Segoe UI Black", max(22, int(24 * s)), "bold"),
            )

        max_name_chars = max(20, int((art_width - text_x - 12) / max(5.5, 6.6 * s)) * 2)
        display_name = item["name"]
        if len(display_name) > max_name_chars:
            display_name = display_name[: max_name_chars - 1].rstrip() + "…"
        identity_panel.create_text(
            text_x,
            int(49 * s),
            text=display_name,
            fill=TEXT,
            anchor="nw",
            justify="left",
            width=max(100, art_width - text_x - 12),
            font=("Segoe UI", max(10, int(12 * s)), "bold"),
        )
        identity_panel.create_text(
            text_x,
            art_height - max(14, int(15 * s)),
            text=compact_display_path(item.get("path", "")),
            fill="#9aabc4",
            anchor="w",
            font=("Consolas", max(7, int(7 * s))),
        )
    else:
        meta_row = tk.Frame(card, bg=CARD)
        meta_row.pack(fill="x", pady=(0, 9))
        surface_widgets.append(meta_row)
        tk.Label(
            meta_row,
            text=f"  {category_text}  ",
            bg=category_color,
            fg=TEXT,
            font=("Segoe UI", max(7, int(7 * s)), "bold"),
            pady=2,
        ).pack(side="left")
        if item["pinned"]:
            meta_right = tk.Label(
                meta_row,
                text="◆  PINNED",
                bg=CARD,
                fg=ACCENT2,
                font=("Segoe UI", max(7, int(7 * s)), "bold"),
            )
        else:
            meta_right = tk.Label(
                meta_row,
                text=extension,
                bg=CARD,
                fg=MUTED,
                font=("Consolas", max(7, int(7 * s)), "bold"),
            )
        selection_box(meta_row).pack(side="right", padx=(5, 0))
        meta_right.pack(side="right")
        surface_widgets.append(meta_right)

        hero_row = tk.Frame(card, bg=CARD)
        hero_row.pack(fill="x", pady=(0, 10))
        surface_widgets.append(hero_row)
        icon = get_icon(item["icon"], icon_size)
        if icon:
            icon_label = tk.Label(
                hero_row,
                image=icon,
                bg=CARD,
                bd=0,
                highlightthickness=0,
                padx=0,
                pady=0,
            )
            icon_label.image = icon
            icon_label.pack(side="left", padx=(0, 11))
        else:
            letter = item["name"][0].upper() if item["name"] else "?"
            icon_label = tk.Label(
                hero_row,
                text=letter,
                bg=CARD,
                fg=color_lite,
                font=("Segoe UI Black", max(22, int(24 * s)), "bold"),
                width=2,
                height=2,
                bd=0,
                highlightthickness=0,
            )
            icon_label.pack(side="left", padx=(0, 11))
        surface_widgets.append(icon_label)

        title_column = tk.Frame(hero_row, bg=CARD)
        title_column.pack(side="left", fill="both", expand=True)
        surface_widgets.append(title_column)
        name_label = tk.Label(
            title_column,
            text=item["name"],
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", max(10, int(12 * s)), "bold"),
            wraplength=int(210 * s),
            justify="left",
            anchor="w",
        )
        name_label.pack(fill="x", pady=(3, 4))
        surface_widgets.append(name_label)
        path_label = tk.Label(
            title_column,
            text=compact_display_path(item.get("path", "")),
            bg=CARD,
            fg=MUTED,
            font=("Consolas", max(7, int(7 * s))),
            anchor="w",
            justify="left",
        )
        path_label.pack(fill="x")
        surface_widgets.append(path_label)

    item_sessions = running_sessions_for_item(item) if active_tab in ("games", "apps") else []
    item_running = bool(item_sessions)
    item_ending = any(session.get("end_requested") for _pid, session in item_sessions)
    item_launching = is_item_launching(item)
    if item_launching:
        status_text, status_color = "◌  STARTING", ORANGE
    elif item_ending:
        status_text, status_color = "◌  ENDING", ORANGE
    elif item_running:
        status_text, status_color = "●  RUNNING", CYAN
    else:
        status_text, status_color = "●  READY", GREEN
    info_row = tk.Frame(card, bg=CARD2, padx=8, pady=5)
    info_row.pack(fill="x", pady=(0, 9))
    status_label = tk.Label(
        info_row,
        text=status_text,
        bg=CARD2,
        fg=status_color,
        font=("Segoe UI", max(7, int(7 * s)), "bold"),
    )
    status_label.pack(side="left")
    playtime_label = tk.Label(
        info_row,
        text="◷  " + format_time(item["playtime"]),
        bg=CARD2,
        fg=SUBTEXT,
        font=("Segoe UI", max(7, int(8 * s)), "bold"),
    )
    playtime_label.pack(side="right")

    if active_tab == "games":
        play_text = "▶  PLAY NOW"
    elif active_tab == "apps":
        play_text = "↗  OPEN APP"
    else:
        play_text = "▶  LAUNCH"
    play_btn = tk.Button(
        card,
        text="STARTING…" if item_launching else play_text,
        state="disabled" if item_launching else "normal",
        bg=color,
        fg=TEXT,
        relief="flat",
        font=("Segoe UI", max(9, int(10 * s)), "bold"),
        pady=max(7, int(7 * s)),
        cursor="hand2",
        bd=0,
        activebackground=color_lite,
        activeforeground=TEXT,
        command=lambda entry=item: run_only(entry),
    )
    play_btn.pack(fill="x", pady=(0, 5))
    bind_animated_button(play_btn, color, color_lite, TEXT, TEXT)

    if active_tab == "games" and item.get("trainer"):
        trainer_btn = tk.Button(
            card,
            text="⚡  LAUNCH WITH TRAINER",
            bg=ORANGE,
            fg=TEXT,
            relief="flat",
            font=("Segoe UI", max(8, int(8 * s)), "bold"),
            pady=max(5, int(5 * s)),
            cursor="hand2",
            bd=0,
            command=lambda entry=item: run_with_trainer(entry),
        )
        trainer_btn.pack(fill="x", pady=(0, 4))
        bind_animated_button(trainer_btn, ORANGE, ORANGE_HOVER, TEXT, TEXT)

    tk.Frame(card, bg=BORDER_SOFT, height=1).pack(fill="x", pady=(7, 7))

    management_row = tk.Frame(card, bg=CARD)
    management_row.pack(fill="x", pady=(0, 5))
    make_action_btn(
        management_row,
        "↻  CHANGE LOCATION",
        CARD2,
        SUBTEXT,
        ACCENT,
        TEXT,
        lambda entry=item: change_item_location(entry),
        scale=s,
    ).pack(side="left", expand=True, fill="x", padx=(0, 3))
    icon_action_btn = make_action_btn(
        management_row,
        "＋  ADD ICON",
        CARD2,
        SUBTEXT,
        CYAN,
        TEXT,
        lambda entry=item: add_custom_icon(entry),
        scale=s,
    )
    if not HAS_PIL:
        icon_action_btn.config(state="disabled", disabledforeground=MUTED)
    icon_action_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
    surface_widgets.append(management_row)

    if active_tab == "founded":
        action_row = make_founded_action_btn(card, item)
    else:
        action_row = tk.Frame(card, bg=CARD)
        action_row.pack(fill="x")
        pin_text = "◆  UNPIN" if item["pinned"] else "◇  PIN TO TOP"
        make_action_btn(
            action_row,
            pin_text,
            CARD2,
            SUBTEXT,
            ACCENT,
            TEXT,
            lambda entry=item: toggle_pin(entry),
            scale=s,
        ).pack(side="left", expand=True, fill="x", padx=(0, 3))
        end_bg = RED if item_running and not item_ending else CARD2
        end_fg = TEXT if item_running and not item_ending else MUTED
        end_btn = make_action_btn(
            action_row,
            "■  END TASK",
            end_bg,
            end_fg,
            RED,
            TEXT,
            lambda entry=item: end_task(entry),
            scale=s,
        )
        end_btn.config(
            state="normal" if item_running and not item_ending else "disabled",
            disabledforeground=MUTED,
        )
        end_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
    surface_widgets.append(action_row)
    bind_context_tree(card, item)
    bind_card_animation(card, stripe, color, surface_widgets, card_margin)
    view.bind_selection(card, item)
    if animate:
        animate_card_entrance(card, stripe, color, min(i * 14, 180))

    live_state = {}

    def update_live(current, checked):
        sessions = running_sessions_for_item(current) if active_tab in ("games", "apps") else []
        running = bool(sessions)
        ending = any(session.get("end_requested") for _pid, session in sessions)
        launching_now = is_item_launching(current)
        text, foreground = (
            ("◌  STARTING", ORANGE)
            if launching_now
            else (
                ("◌  ENDING", ORANGE)
                if ending
                else (("●  RUNNING", CYAN) if running else ("●  READY", GREEN))
            )
        )
        state = (
            text,
            foreground,
            current.get("playtime", 0),
            launching_now,
            running,
            ending,
            bool(checked),
        )
        if live_state.get("state") == state:
            return
        old = live_state.get("state")
        if old is None or old[:2] != state[:2]:
            status_label.configure(text=text, fg=foreground)
        if old is None or old[2] != state[2]:
            playtime_label.configure(text="◷  " + format_time(current.get("playtime", 0)))
        if old is None or old[3] != state[3]:
            play_btn.configure(
                text="STARTING…" if launching_now else play_text,
                state="disabled" if launching_now else "normal",
            )
            if trainer_btn is not None:
                trainer_btn.configure(state="disabled" if launching_now else "normal")
        if end_btn is not None and (old is None or old[4:6] != state[4:6]):
            enabled = running and not ending
            end_btn.configure(
                state="normal" if enabled else "disabled",
                bg=RED if enabled else CARD2,
                fg=TEXT if enabled else MUTED,
            )
            end_btn._anim_idle_bg = RED if enabled else CARD2
            end_btn._anim_idle_fg = TEXT if enabled else MUTED
        if old is None or old[6] != state[6]:
            selection_var.set(bool(checked))
            card._library_selected = bool(checked)
            animate_widget_color(
                card, "highlightbackground", NEON if checked else BORDER_SOFT, 80, 5, BORDER_SOFT
            )
        live_state["state"] = state

    return CardHandle(card, update_live, cancel_widget_tree_animations)


def _library_card_signature(item, layout, context):
    return (
        context,
        layout["columns"],
        layout["width"],
        layout["scale"],
        item.get("name"),
        item.get("path"),
        item.get("trainer"),
        item.get("icon"),
        item.get("artwork"),
        item.get("color"),
        bool(item.get("pinned")),
        bool(launcher_settings.get("card_art_enabled", True)),
    )


def display_items(item_list):
    global library_view
    width = root.winfo_width()
    columns = 1 if width < 600 else (2 if width < 1000 else (3 if width < 1400 else 4))
    if library_view is None or library_view.closed or library_view.parent is not frame:
        clear_item_widgets()
        library_view = LibraryView(
            frame,
            canvas,
            _build_library_card,
            _library_card_signature,
            change_page,
            bulk_library_action,
            background=BG,
        )
    for column in range(4):
        frame.grid_columnconfigure(
            column,
            weight=1 if column < columns else 0,
            uniform="group1" if column < columns else "",
        )
    total = len(item_list)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page_by_tab[active_tab], pages - 1))
    page_by_tab[active_tab] = page
    visible = item_list[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    with data_lock:
        all_items = list(current_list())
    library_view.render(
        all_items,
        visible,
        context=active_tab,
        layout={"width": width, "columns": columns, "scale": current_scale},
        page=page,
        total_pages=pages,
        total_items=total,
        background=BG,
    )


# ==========================================
# WINDOWS EFFECTS
# ==========================================
def enable_win11_round_corners(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(DWMWCP_ROUND)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except (AttributeError, OSError, tk.TclError) as exc:
        LOG.debug("Rounded corners unavailable: %s", exc)
        return False


def enable_glass_effect(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())

        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId", ctypes.c_int),
            ]

        class DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENTPOLICY(4, 0, 0, 0)
        data = DATA(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        result = ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        return bool(result)
    except (AttributeError, OSError, TypeError, tk.TclError) as exc:
        LOG.debug("Glass effect unavailable: %s", exc)
        return False


def enable_window_shadow(window):
    if os.name != "nt":
        return None
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        class_style = ctypes.windll.user32.GetClassLongW(hwnd, -26)
        ctypes.windll.user32.SetClassLongW(hwnd, -26, class_style | 0x00020000)
        return True
    except (AttributeError, OSError, tk.TclError) as exc:
        LOG.debug("Window shadow unavailable: %s", exc)
        return False


# ==========================================
# SPLASH SCREEN
# ==========================================
def show_splash(window, video_path="intro.mp4", width=600, height=350, duration=900):
    """Show a safe Tk-only splash; native video codecs never gate startup."""
    del video_path  # The MP4 is retained as an asset but is not decoded at startup.
    try:
        window.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    window.withdraw()

    splash = tk.Toplevel(window)
    splash.overrideredirect(True)
    splash.configure(bg=BG)
    try:
        splash.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    x = (splash.winfo_screenwidth() - width) // 2
    y = (splash.winfo_screenheight() - height) // 2
    splash.geometry(f"{width}x{height}+{x}+{y}")

    panel = tk.Canvas(splash, width=width, height=height, bg=BG, bd=0, highlightthickness=0)
    panel.pack(fill="both", expand=True)
    panel.create_rectangle(0, 0, width, height, fill=BG, outline="")
    panel.create_rectangle(0, 0, 8, height, fill=ACCENT, outline="")
    panel.create_line(36, 64, width - 36, 64, fill=BORDER, width=1)
    panel.create_line(36, height - 62, width - 36, height - 62, fill=BORDER, width=1)
    panel.create_text(
        width // 2,
        126,
        text="XVVIIX",
        fill=TEXT,
        font=("Segoe UI Black", 44, "bold"),
    )
    panel.create_text(
        width // 2,
        177,
        text="XVVIIX  //  COMMAND CENTER",
        fill=NEON,
        font=("Segoe UI", 12, "bold"),
    )
    panel.create_text(
        width // 2,
        213,
        text="INITIALIZING LAUNCH SYSTEMS",
        fill=SUBTEXT,
        font=("Consolas", 10, "bold"),
    )
    panel.create_rectangle(130, 248, width - 130, 254, fill=BORDER_SOFT, outline="")
    panel.create_rectangle(130, 248, width - 210, 254, fill=ACCENT, outline="")
    panel.create_text(
        width // 2,
        height - 32,
        text="LAUNCH  •  TRACK  •  DISCOVER",
        fill=MUTED,
        font=("Segoe UI", 8, "bold"),
    )

    state = {"closed": False}

    def fade_in_splash(alpha=0.0):
        if state["closed"] or not splash.winfo_exists():
            return
        try:
            splash.attributes("-alpha", min(alpha, 1.0))
        except tk.TclError:
            return
        if alpha < 1.0:
            splash.after(20, fade_in_splash, alpha + 0.08)

    def fade_in_root(alpha=0.0):
        window.deiconify()
        try:
            window.attributes("-alpha", min(alpha, 1.0))
        except tk.TclError:
            return
        if alpha < 1.0:
            window.after(20, fade_in_root, alpha + 0.08)

    def close_splash():
        if state["closed"]:
            return
        state["closed"] = True
        try:
            splash.destroy()
        except tk.TclError:
            pass
        fade_in_root()

    splash.protocol("WM_DELETE_WINDOW", close_splash)
    splash.bind("<Button-1>", lambda _event: close_splash())
    splash.bind("<Escape>", lambda _event: close_splash())
    fade_in_splash()
    splash.after(duration, close_splash)


# ==========================================
# MAIN WINDOW CREATION
# ==========================================
def main():
    global root, canvas, frame, tab_bar, search_entry, name_entry, add_btn, sort_btn
    global \
        stats_lbl, \
        games_tab_btn, \
        apps_tab_btn, \
        founded_tab_btn, \
        reports_tab_btn, \
        monitor_tab_btn, \
        search_var
    global scan_btn, bg_btn, clock_lbl, music_btn, is_fullscreen, hardware_monitor
    global hardware_monitor_state, hardware_monitor_error
    global drop_zone, drop_container, topbar, container, DND_ACTIVE
    global header_canvas, header_image_ref, header_stats_id, activity_rail
    global activity_primary_lbl, activity_secondary_lbl
    global background_music, launcher_settings
    startup_checkpoint(
        "APPLICATION_ENTRY",
        "STARTED",
        f"platform={sys.platform}; frozen={bool(getattr(sys, 'frozen', False))}",
    )
    print("Initializing launcher window...", flush=True)
    dnd_enabled = HAS_DND
    try:
        if dnd_enabled:
            try:
                root = TkinterDnD.Tk()
            except Exception as exc:
                LOG.warning("Drag-and-drop initialization failed; using plain Tk: %s", exc)
                dnd_enabled = False
                root = tk.Tk()
        else:
            root = tk.Tk()
    except tk.TclError as exc:
        startup_checkpoint("WINDOW_BACKEND", "FAILED", f"Tk window creation failed: {exc}")
        report_startup_error(f"Could not create the application window:\n{exc}")
        return 1

    DND_ACTIVE = dnd_enabled
    root.withdraw()
    startup_checkpoint(
        "WINDOW_BACKEND",
        "READY" if dnd_enabled else "DEGRADED",
        f"backend={'tkinterdnd2' if dnd_enabled else 'tkinter'}; root=created; initial_state=withdrawn",
    )
    if not dnd_enabled:
        LOG.warning("tkinterdnd2 is unavailable; drag and drop is disabled.")
    if not HAS_PIL:
        LOG.warning("Pillow is unavailable; custom icons and video frames are disabled.")
    if not HAS_PSUTIL:
        LOG.warning("psutil is unavailable; playtime monitoring is disabled.")
    runtime_degraded = [
        name
        for name, available in (
            ("drag_drop", dnd_enabled),
            ("pillow", HAS_PIL),
            ("psutil", HAS_PSUTIL),
            ("hardware_monitor", HAS_HARDWARE_MONITOR),
            ("cryptography", HAS_CRYPTOGRAPHY),
        )
        if not available
    ]
    startup_checkpoint(
        "RUNTIME_CAPABILITIES",
        "DEGRADED" if runtime_degraded else "READY",
        "unavailable=" + (",".join(runtime_degraded) if runtime_degraded else "none"),
    )

    launcher_settings = load_launcher_settings()
    startup_checkpoint(
        "SETTINGS",
        settings_load_status,
        f"{settings_load_detail}; music_enabled={launcher_settings['music_enabled']}; card_art={launcher_settings['card_art_enabled']}",
    )
    background_music = audio.BackgroundMusic(
        MUSIC_FILE,
        enabled=launcher_settings["music_enabled"],
        volume=launcher_settings["music_volume"],
        title=MUSIC_TITLE,
        checkpoint=startup_checkpoint,
        on_unavailable=lambda: post_ui(update_music_button),
        logger=LOG,
    )
    if not background_music.available:
        LOG.warning("Background music is unavailable; install pygame and verify the music asset.")
    startup_checkpoint(
        "AUDIO_CONTROLLER",
        "READY" if background_music.available else "DEGRADED",
        f"pygame={'deferred' if not audio._pygame_import_attempted else ('ready' if audio.HAS_PYGAME else 'unavailable')}; asset={os.path.isfile(MUSIC_FILE)}; enabled={background_music.enabled}",
    )

    try:
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except (OSError, tk.TclError) as exc:
        LOG.debug("Window icon unavailable: %s", exc)

    root.configure(bg="#000000")
    if not initialize_data_vault(root):
        startup_checkpoint(
            "DATA_VAULT_AND_LIBRARIES", "ABORTED", "vault setup or unlock did not complete"
        )
        vault.clear_vault_key()
        try:
            stop_card_artwork()
            cancel_pending_callbacks(root)
            root.destroy()
        except tk.TclError:
            pass
        return 0
    root.update_idletasks()
    startup_checkpoint(
        "DATA_VAULT_AND_LIBRARIES",
        "READY",
        f"encrypted=true; games={len(games)}; apps={len(apps)}; discovered={len(founded)}; reports={len(system_report_items())}; activity={len(recent_activity)}; recovery_events={len(vault.load_warnings)}",
    )

    glass_ready = enable_glass_effect(root)
    corners_ready = enable_win11_round_corners(root)
    shadow_ready = enable_window_shadow(root)

    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.after(100, enable_glass_effect, root)
    native_effects = {
        "glass": glass_ready,
        "rounded_corners": corners_ready,
        "shadow": shadow_ready,
    }
    startup_checkpoint(
        "NATIVE_WINDOW_EFFECTS",
        "SKIPPED" if os.name != "nt" else ("READY" if all(native_effects.values()) else "DEGRADED"),
        "Windows-only effects not required on this platform"
        if os.name != "nt"
        else "; ".join(
            f"{name}={'ready' if available else 'unavailable'}"
            for name, available in native_effects.items()
        ),
    )

    splash_status = "READY"
    splash_detail = "intro window scheduled"
    splash_duration = 350
    try:
        video_intro_path = resource_path("intro.mp4")
        show_splash(
            root,
            video_path=video_intro_path,
            width=600,
            height=350,
            duration=splash_duration,
        )
    except (OSError, RuntimeError, tk.TclError) as exc:
        splash_status = "DEGRADED"
        splash_detail = f"intro disabled; main window fallback active: {exc}"
        LOG.warning("Splash screen disabled after an error: %s", exc)
        root.deiconify()
        try:
            root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
    startup_checkpoint("SPLASH_PRESENTATION", splash_status, splash_detail)

    root.title("XVVIIX Launcher")
    root.geometry("1180x820")
    root.minsize(760, 640)

    if sys.platform == "win32":
        try:
            root.wm_attributes("-titlebarcolor", TOP_BG)
        except Exception:
            pass

    is_fullscreen = False

    def toggle_fullscreen(event=None):
        global is_fullscreen
        is_fullscreen = not is_fullscreen
        root.attributes("-fullscreen", is_fullscreen)

    def exit_fullscreen(event=None):
        global is_fullscreen
        is_fullscreen = False
        root.attributes("-fullscreen", False)

    root.bind("<F11>", toggle_fullscreen)
    root.bind("<Escape>", exit_fullscreen)
    root.bind_all(
        "<Control-Alt-m>",
        lambda _event: (
            toggle_hardware_overlay_mode()
            if widget_exists(monitor_overlay)
            else open_hardware_overlay()
        ),
    )
    startup_checkpoint(
        "WINDOW_CONFIGURATION",
        "READY",
        "title=XVVIIX Launcher; geometry=1180x820; minimum=760x640; fullscreen and monitor-overlay bindings=ready",
    )

    # ==========================================
    # UI CONSTRUCTION
    # ==========================================
    header_canvas = tk.Canvas(root, height=156, bg=TOP_BG, highlightthickness=0, bd=0)
    header_canvas.pack(fill="x")

    header_image_id = None
    if HAS_PIL:
        try:
            header_image = Image.open(resource_path("assets/xvviix_header.png")).convert("RGB")
            target_width, crop_height = 1920, 190
            if header_image.size != (target_width, crop_height):
                target_height = int(header_image.height * target_width / header_image.width)
                header_image = header_image.resize((target_width, target_height), Image.LANCZOS)
                crop_top = max(0, (target_height - crop_height) // 2)
                header_image = header_image.crop(
                    (0, crop_top, target_width, crop_top + crop_height)
                )
            header_image_ref = ImageTk.PhotoImage(header_image)
            header_image_id = header_canvas.create_image(
                0, 78, image=header_image_ref, anchor="center"
            )
        except (OSError, ValueError) as exc:
            LOG.warning("Header artwork unavailable: %s", exc)

    header_canvas.create_rectangle(0, 0, 9, 156, fill=ACCENT, outline="")
    header_canvas.create_text(
        34, 31, text="✦  XVVIIX", anchor="w", fill=TEXT, font=("Segoe UI Black", 27, "bold")
    )
    header_canvas.create_text(
        36,
        66,
        text="XVVIIX  //  COMMAND CENTER",
        anchor="w",
        fill=NEON,
        font=("Segoe UI", 10, "bold"),
    )
    header_canvas.create_text(
        36,
        90,
        text="LAUNCH  •  TRACK  •  DISCOVER",
        anchor="w",
        fill=SUBTEXT,
        font=("Segoe UI", 9, "bold"),
    )
    header_stats_id = header_canvas.create_text(
        36, 121, text="", anchor="w", fill="#c4b5fd", font=("Segoe UI", 9, "bold")
    )

    activity_rail = tk.Frame(
        header_canvas,
        bg="#0b1528",
        highlightbackground=BORDER,
        highlightthickness=1,
        cursor="hand2",
    )
    rail_heading = tk.Frame(activity_rail, bg="#0b1528")
    rail_heading.pack(fill="x", padx=11, pady=(7, 2))
    tk.Label(
        rail_heading,
        text="RECENT ACTIVITY  //  LIVE",
        bg="#0b1528",
        fg=NEON,
        font=("Consolas", 7, "bold"),
    ).pack(side="left")
    tk.Label(
        rail_heading,
        text="OPEN HISTORY  ›",
        bg="#0b1528",
        fg=MUTED,
        font=("Segoe UI", 7, "bold"),
    ).pack(side="right")
    activity_primary_lbl = tk.Label(
        activity_rail,
        text="",
        bg="#0b1528",
        fg=TEXT,
        font=("Segoe UI", 8, "bold"),
        anchor="w",
    )
    activity_primary_lbl.pack(fill="x", padx=11, pady=(2, 1))
    activity_secondary_lbl = tk.Label(
        activity_rail,
        text="",
        bg="#0b1528",
        fg=SUBTEXT,
        font=("Segoe UI", 7),
        anchor="w",
    )
    activity_secondary_lbl.pack(fill="x", padx=11, pady=(0, 6))
    for rail_widget in (
        activity_rail,
        rail_heading,
        *rail_heading.winfo_children(),
        activity_primary_lbl,
        activity_secondary_lbl,
    ):
        rail_widget.bind("<Button-1>", show_recent_activity, add="+")

    clock_lbl = tk.Label(
        header_canvas,
        text="",
        bg="#0b1528",
        fg=NEON,
        font=("Consolas", 12, "bold"),
        padx=13,
        pady=7,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    music_btn = tk.Button(
        header_canvas,
        text="♫  GALACTIC  ON",
        bg="#10243a",
        fg=NEON,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        padx=13,
        pady=7,
        cursor="hand2",
        bd=0,
        command=toggle_background_music,
        activebackground="#164e63",
        activeforeground=TEXT,
    )
    bg_btn = tk.Button(
        header_canvas,
        text="◈  SETTINGS",
        bg=BG2,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        padx=14,
        pady=9,
        cursor="hand2",
        bd=0,
        command=show_theme_menu,
    )
    bind_animated_button(music_btn, "#10243a", "#164e63", NEON, TEXT)
    bind_animated_button(bg_btn, BG2, CARD2, SUBTEXT, TEXT)
    music_btn.bind("<Button-3>", show_music_credit, add="+")
    activity_window = header_canvas.create_window(
        0,
        0,
        window=activity_rail,
        anchor="nw",
        width=420,
        height=80,
    )
    clock_window = header_canvas.create_window(0, 0, window=clock_lbl)
    music_window = header_canvas.create_window(0, 0, window=music_btn)
    music_credit_id = header_canvas.create_text(
        0,
        0,
        text="GALACTIC ODYSSEY  •  MUSIC BY ALKAKRAB",
        anchor="center",
        fill=MUTED,
        font=("Segoe UI", 7, "bold"),
    )
    theme_window = header_canvas.create_window(0, 0, window=bg_btn)
    update_music_button()

    def layout_header(event=None):
        width = event.width if event is not None else header_canvas.winfo_width()
        if header_image_id is not None:
            header_canvas.coords(header_image_id, width // 2, 78)
        music_x = max(410, width - 230)
        if width >= 900:
            rail_left = 315 if width < 1050 else 335
            rail_right = music_x - 100
            rail_width = max(235, rail_right - rail_left)
            header_canvas.itemconfigure(activity_window, state="normal", width=rail_width)
            header_canvas.coords(activity_window, rail_left, 13)
        else:
            header_canvas.itemconfigure(activity_window, state="hidden")
        header_canvas.coords(clock_window, max(510, width - 88), 33)
        header_canvas.coords(music_window, music_x, 31)
        header_canvas.coords(music_credit_id, music_x, 61)
        header_canvas.coords(theme_window, max(650, width - 66), 107)

    header_canvas.bind("<Configure>", layout_header)

    def update_clock():
        clock_lbl.config(text=time.strftime("%H:%M:%S"))
        root.after(1000, update_clock)

    update_clock()

    accent_line = tk.Frame(root, bg=ACCENT, height=3)
    accent_line.pack(fill="x")
    pulse_widget(accent_line, ACCENT, ACCENT2, cycles=2, duration=220)
    startup_checkpoint(
        "HEADER_INTERFACE",
        "READY" if header_image_id is not None or not HAS_PIL else "DEGRADED",
        f"identity=ready; activity_rail=ready; clock=ready; artwork={'ready' if header_image_id is not None else 'fallback'}",
    )

    tab_bar = tk.Frame(root, bg=BG, pady=12)
    tab_bar.pack(fill="x", padx=30)
    tk.Label(
        tab_bar,
        text="LIBRARY",
        bg=BG,
        fg=MUTED,
        font=("Segoe UI", 9, "bold"),
        padx=4,
    ).pack(side="left", padx=(0, 14))

    games_tab_btn = tk.Button(
        tab_bar,
        text="◉  GAMES",
        bg=TAB_ACT,
        fg=TEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=22,
        pady=9,
        cursor="hand2",
        bd=0,
        command=lambda: switch_tab("games"),
    )
    games_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(games_tab_btn, TAB_ACT, ACCENT2, TEXT, TEXT)

    apps_tab_btn = tk.Button(
        tab_bar,
        text="◇  WORKSPACE",
        bg=TAB_IN,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=22,
        pady=9,
        cursor="hand2",
        bd=0,
        command=lambda: switch_tab("apps"),
    )
    apps_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(apps_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    founded_tab_btn = tk.Button(
        tab_bar,
        text="✦  DISCOVERED",
        bg=TAB_IN,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=18,
        pady=9,
        cursor="hand2",
        bd=0,
        command=lambda: switch_tab("founded"),
    )
    founded_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(founded_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    reports_tab_btn = tk.Button(
        tab_bar,
        text="△  REPORTS",
        bg=TAB_IN,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=18,
        pady=9,
        cursor="hand2",
        bd=0,
        command=lambda: switch_tab("reports"),
    )
    reports_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(reports_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)

    monitor_tab_btn = tk.Button(
        tab_bar,
        text="⌁  MONITOR",
        bg=TAB_IN,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 10, "bold"),
        padx=18,
        pady=9,
        cursor="hand2",
        bd=0,
        command=lambda: switch_tab("monitor"),
    )
    monitor_tab_btn.pack(side="left", padx=(0, 8))
    bind_animated_button(monitor_tab_btn, TAB_IN, CARD2, SUBTEXT, TEXT)
    startup_checkpoint(
        "NAVIGATION_INTERFACE",
        "READY",
        "tabs=games,workspace,discovered,reports,monitor; active=games",
    )

    drop_container = tk.Frame(root, bg=BG, padx=30)
    drop_container.pack(fill="x", pady=(0, 10))
    drop_zone = tk.Label(
        drop_container,
        text=default_drop_text(),
        bg=BG2,
        fg=SUBTEXT,
        font=("Segoe UI", 9, "bold"),
        pady=7,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    drop_zone.pack(fill="x")
    animate_widget_color(drop_zone, "highlightbackground", ACCENT, 260, 12, BORDER)
    root.after(280, animate_widget_color, drop_zone, "highlightbackground", BORDER, 320, 12, ACCENT)

    topbar = tk.Frame(
        root, bg=CARD, padx=20, pady=13, highlightbackground=BORDER_SOFT, highlightthickness=1
    )
    topbar.pack(fill="x", padx=30, pady=(0, 14))

    scan_btn = tk.Button(
        topbar,
        text="⌁  SCAN SYSTEM",
        bg=GREEN,
        fg=TEXT,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        padx=15,
        pady=9,
        cursor="hand2",
        bd=0,
        command=start_auto_scan,
        activebackground=GREEN_HOVER,
        activeforeground=TEXT,
    )
    scan_btn.pack(side="left", padx=(0, 16))
    bind_animated_button(scan_btn, GREEN, GREEN_HOVER, TEXT, TEXT, sound=True)

    tk.Label(topbar, text="⌕  SEARCH", bg=CARD, fg=NEON, font=("Segoe UI", 9, "bold")).pack(
        side="left"
    )

    search_var = DialogVariable(root)
    search_var.trace_add("write", search_games)

    search_entry = tk.Entry(
        topbar,
        textvariable=search_var,
        bg=BG2,
        fg=TEXT,
        insertbackground=NEON,
        relief="flat",
        highlightbackground=BORDER,
        highlightthickness=1,
        font=("Segoe UI", 11),
    )
    search_entry.pack(side="left", padx=12, ipady=8)
    search_entry.bind("<FocusIn>", lambda _event: animate_entry_focus(search_entry, True))
    search_entry.bind("<FocusOut>", lambda _event: animate_entry_focus(search_entry, False))

    tk.Label(topbar, text="NEW ENTRY", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(
        side="left", padx=(24, 6)
    )

    name_entry = tk.Entry(
        topbar,
        bg=BG2,
        fg=TEXT,
        insertbackground=NEON,
        relief="flat",
        highlightbackground=BORDER,
        highlightthickness=1,
        font=("Segoe UI", 11),
    )
    name_entry.pack(side="left", padx=5, ipady=8)
    name_entry.bind("<FocusIn>", lambda _event: animate_entry_focus(name_entry, True))
    name_entry.bind("<FocusOut>", lambda _event: animate_entry_focus(name_entry, False))

    add_btn = tk.Button(
        topbar,
        text="＋  ADD ENTRY",
        bg=ACCENT,
        fg=TEXT,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        padx=18,
        pady=9,
        cursor="hand2",
        bd=0,
        command=add_item,
    )
    add_btn.pack(side="left", padx=15)
    bind_animated_button(add_btn, ACCENT, ACCENT2, TEXT, TEXT)

    sort_btn = tk.Button(
        topbar,
        text="⇅  PINNED FIRST",
        bg=BG2,
        fg=SUBTEXT,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        padx=15,
        pady=9,
        cursor="hand2",
        bd=0,
        command=change_sort,
    )
    sort_btn.pack(side="right", padx=10)
    bind_animated_button(sort_btn, BG2, CARD2, SUBTEXT, TEXT)
    startup_checkpoint(
        "COMMAND_CONTROLS",
        "READY",
        "system_scan=ready; search=ready; add_entry=ready; sort=ready; drop_zone=created",
    )

    container = tk.Frame(root, bg=BG)
    container.pack(fill="both", expand=True, padx=20, pady=(0, 5))

    canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
    canvas.pack(side="left", fill="both", expand=True)

    xvviix_style = ttk.Style(root)
    try:
        xvviix_style.theme_use("clam")
    except tk.TclError:
        pass
    xvviix_style.configure(
        "XVVIIX.Vertical.TScrollbar",
        troughcolor=BG,
        background=CARD2,
        bordercolor=BG,
        darkcolor=CARD2,
        lightcolor=CARD2,
        arrowcolor=SUBTEXT,
        relief="flat",
        width=10,
    )
    xvviix_style.map("XVVIIX.Vertical.TScrollbar", background=[("active", ACCENT)])
    scrollbar = ttk.Scrollbar(
        container,
        orient="vertical",
        command=_scroll_library,
        style="XVVIIX.Vertical.TScrollbar",
    )
    scrollbar.pack(side="right", fill="y", padx=(6, 0))

    canvas.configure(yscrollcommand=scrollbar.set)

    frame = tk.Frame(canvas, bg=BG)
    frame_id = canvas.create_window((0, 0), window=frame, anchor="nw")

    frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(frame_id, width=e.width))

    if DND_ACTIVE:
        try:
            for target in (root, drop_zone, canvas, frame):
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<DropEnter>>", handle_drop_enter)
                target.dnd_bind("<<DropLeave>>", handle_drop_leave)
                target.dnd_bind("<<Drop>>", handle_drop_event)
        except (tk.TclError, AttributeError) as exc:
            LOG.warning("Drag-and-drop registration failed: %s", exc)
            DND_ACTIVE = False
            reset_drop_status()

    startup_checkpoint(
        "CONTENT_CANVAS",
        "READY",
        "scroll_canvas=ready; responsive_frame=ready; themed_scrollbar=ready",
    )
    startup_checkpoint(
        "INPUT_INTEGRATIONS",
        "READY" if DND_ACTIVE else ("DEGRADED" if HAS_DND else "SKIPPED"),
        "drag_and_drop=registered"
        if DND_ACTIVE
        else (
            "drag_and_drop=registration_failed" if HAS_DND else "drag_and_drop=package_unavailable"
        ),
    )

    root.bind("<Configure>", update_scale)
    root.bind_all(
        "<MouseWheel>",
        _wheel_library,
    )

    stats_frame = tk.Frame(
        root, bg=CARD, pady=8, highlightbackground=BORDER_SOFT, highlightthickness=1
    )
    stats_frame.pack(side="bottom", fill="x")
    tk.Label(
        stats_frame,
        text="●  SYSTEM ONLINE",
        bg=CARD,
        fg=GREEN,
        font=("Segoe UI", 8, "bold"),
    ).pack(side="left", padx=24)
    stats_lbl = tk.Label(
        stats_frame,
        text="",
        bg=CARD,
        fg=NEON,
        font=("Segoe UI", 9, "bold"),
    )
    stats_lbl.pack(side="left", expand=True)
    tk.Label(
        stats_frame,
        text="F11  FULLSCREEN     ESC  EXIT",
        bg=CARD,
        fg=MUTED,
        font=("Consolas", 8, "bold"),
    ).pack(side="right", padx=24)
    startup_checkpoint(
        "STATUS_INTERFACE",
        "READY",
        "system state, aggregate statistics, fullscreen, and exit indicators created",
    )

    root.update_idletasks()
    enable_win11_round_corners(root)

    apply_topbar_scaling()
    refresh()
    startup_checkpoint(
        "INITIAL_RENDER",
        "READY",
        f"active_tab={active_tab}; visible_records={len(games)}; responsive_scale={current_scale:.2f}",
    )
    process_ui_queue()
    startup_checkpoint("UI_DISPATCH_QUEUE", "READY", "worker result queue scheduled on Tk thread")

    if hardware_monitor is not None:
        hardware_monitor.stop()
    hardware_monitor = None
    hardware_monitor_error = ""
    if HAS_HARDWARE_MONITOR:
        hardware_monitor_state = "standby"
        startup_checkpoint(
            "HARDWARE_MONITOR",
            "READY",
            "on-demand standby; zero telemetry overhead until Monitor or overlay opens",
        )
    else:
        hardware_monitor_state = "unavailable"
        startup_checkpoint(
            "HARDWARE_MONITOR", "SKIPPED", "integrated monitor backend or psutil unavailable"
        )

    if background_music.enabled:

        def start_audio_after_first_paint():
            threading.Thread(
                target=background_music.start,
                args=(True,),
                daemon=True,
                name="background-audio-loader",
            ).start()

        root.after(splash_duration + 120, start_audio_after_first_paint)
    else:
        startup_checkpoint("AUDIO_PLAYBACK", "SKIPPED", "disabled by launcher setting")

    if vault.load_warnings:
        root.after(
            250,
            messagebox.showwarning,
            "Library recovery",
            "\n\n".join(vault.load_warnings),
        )

    def close_launcher():
        request_launcher_close()

    root.protocol("WM_DELETE_WINDOW", close_launcher)
    root.after(1000, _activity_clock_tick)
    root.after(1200, initialize_windows_startup)
    root.bind("<Map>", _main_monitor_visibility_changed, add="+")
    root.bind("<Unmap>", _main_monitor_visibility_changed, add="+")

    if HAS_PSUTIL:
        monitor_stop.clear()
        monitor_thread = threading.Thread(
            target=monitor_running_apps,
            daemon=True,
            name="playtime-monitor",
        )
        monitor_thread.start()
        print("Monitor thread starting...", flush=True)
        monitor_status = "READY"
        monitor_detail = f"playtime-monitor alive={monitor_thread.is_alive()}"
    else:
        monitor_status = "SKIPPED"
        monitor_detail = "playtime monitor unavailable without psutil"
    startup_checkpoint(
        "ASYNC_SERVICES",
        monitor_status,
        f"{monitor_detail}; hardware_monitor={hardware_monitor_state}; audio={'scheduled' if background_music.enabled else 'disabled'}; recovery_notices={len(vault.load_warnings)}",
    )

    print("BEFORE MAINLOOP", flush=True)
    startup_checkpoint(
        "MAIN_EVENT_LOOP",
        "READY",
        f"Tk callbacks armed; startup_checkpoints={len(startup_diagnostics) + 1}",
    )
    try:
        root.mainloop()
    except Exception as exc:
        startup_checkpoint("MAIN_EVENT_LOOP", "FAILED", f"fatal callback error: {exc}")
        LOG.exception("Fatal main-loop error")
        app_exit_event.set()
        _stop_background_services()
        _finish_launcher_close()
        return 1
    app_exit_event.set()
    _stop_background_services()
    startup_checkpoint("MAIN_EVENT_LOOP", "STOPPED", "Tk event loop exited cleanly")
    return 0


def run():
    try:
        return main()
    except Exception as exc:
        startup_checkpoint("APPLICATION_STARTUP", "FAILED", f"unhandled exception: {exc}")
        LOG.exception("Launcher startup failed")
        try:
            if root is not None:
                stop_card_artwork()
                cancel_pending_callbacks(root)
                root.destroy()
        except (AttributeError, tk.TclError):
            pass
        report_startup_error(f"The launcher could not start:\n{exc}")
        return 1
