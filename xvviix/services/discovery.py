"""Read-only local software discovery and conservative evidence-based classification.

This is a rule engine, not a cloud/ML service. No candidate is executed.
"""

from dataclasses import dataclass, asdict
import ctypes
from ctypes import wintypes
from functools import lru_cache
import ntpath
import os
import re
import stat
import time

from .. import models
from ..utils import clean_path
from . import software_catalog
from .discovery_rules import (
    APP_MARKERS,
    DRIVER_MARKERS,
    GAME_PATH_MARKERS,
    GAME_PUBLISHER_MARKERS,
    KNOWN_LAUNCHER_MARKERS,
    SYSTEM_PATH_MARKERS,
    UNNECESSARY_BASENAMES,
)


class ScanCancelled(Exception):
    pass


@dataclass
class ScanStats:
    roots_total: int = 0
    roots_done: int = 0
    directories: int = 0
    entries: int = 0
    executables: int = 0
    skipped_links: int = 0
    skipped_offline: int = 0
    skipped_system: int = 0
    inaccessible: int = 0
    limit_reached: bool = False


GLOBAL_SKIP_DIRS = frozenset({".git", ".svn", "node_modules", "__pycache__", ".venv", ".cache"})
ROOT_SKIP_DIRS = frozenset(
    {
        "windows",
        "windows.old",
        "$recycle.bin",
        "system volume information",
        "recovery",
        "$winreagent",
    }
)
HELPER_NAMES = UNNECESSARY_BASENAMES | {
    "steamwebhelper.exe",
    "steamservice.exe",
    "unitycrashhandler64.exe",
    "unitycrashhandler32.exe",
    "unrealcefsubprocess.exe",
    "crashreportclient.exe",
    "cefsubprocess.exe",
    "notification_helper.exe",
    "easyanticheat_setup.exe",
    "easyanticheat_eos_setup.exe",
    "vc_redist.arm64.exe",
}
KNOWN_APP_EXES = frozenset(
    {
        "steam.exe",
        "epicgameslauncher.exe",
        "galaxyclient.exe",
        "ubisoftconnect.exe",
        "uplay.exe",
        "eadesktop.exe",
        "riotclientservices.exe",
        "battle.net.exe",
        "unity.exe",
        "unityhub.exe",
        "unrealeditor.exe",
        "ue4editor.exe",
        "code.exe",
        "devenv.exe",
        "blender.exe",
        "chrome.exe",
        "firefox.exe",
        "msedge.exe",
        "discord.exe",
    }
)


def normalized_text(*values):
    return (
        " "
        + " ".join(
            re.findall(
                r"[\w]+",
                " ".join(str(value or "") for value in values).casefold(),
                flags=re.UNICODE,
            )
        )
        + " "
    )


def helper_executable(path):
    name = ntpath.basename(str(path)).casefold()
    stem = ntpath.splitext(name)[0]
    installer_prefix = re.match(
        r"^(?:setup|installer|uninstall|updater|update)(?:[-_.0-9 ]|$)", stem
    )
    return (
        name in software_catalog.RUNTIME_EXES
        or name in HELPER_NAMES
        or name.startswith("unins")
        or name.startswith("vcredist_")
        or bool(installer_prefix)
    )


def scan_modes():
    return ("registered", "search")


@lru_cache(maxsize=32)
def _local_drive_type(drive):
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        return kernel.GetDriveTypeW(drive.rstrip("\\/") + "\\") in (2, 3)
    except (AttributeError, OSError):
        return False


def is_local_scan_path(path):
    """Do not turn a shortcut/registry entry into an implicit network scan."""
    value = clean_path(path)
    normalized = value.replace("/", "\\")
    if normalized.startswith("\\\\"):
        if not normalized.startswith("\\\\?\\") or normalized.casefold().startswith("\\\\?\\unc\\"):
            return False
        normalized = normalized[4:]
    drive = ntpath.splitdrive(normalized)[0]
    return bool(value) and (os.name != "nt" or not drive or _local_drive_type(drive))


def iter_executables(
    roots, *, cancelled=lambda: False, progress=None, stats=None, candidate_limit=100000
):
    """Walk selected local roots without following reparse points or cloud recalls."""
    stats = stats if stats is not None else ScanStats()
    roots = list(dict.fromkeys(os.path.abspath(os.fspath(root)) for root in roots))
    stats.roots_total = len(roots)
    last_notice = 0.0
    for root in roots:
        stack = [(root, True)]
        while stack:
            if cancelled():
                raise ScanCancelled()
            directory, at_root = stack.pop()
            stats.directories += 1
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancelled():
                            raise ScanCancelled()
                        stats.entries += 1
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                            candidate = entry.name.casefold().endswith((".exe", ".bat"))
                            if not is_directory and not candidate:
                                continue
                            info = entry.stat(follow_symlinks=False)
                            attributes = getattr(info, "st_file_attributes", 0)
                            if entry.is_symlink() or attributes & 0x400:
                                stats.skipped_links += 1
                                continue
                            if attributes & (0x1000 | 0x40000 | 0x400000):
                                stats.skipped_offline += 1
                                continue
                            if is_directory:
                                name = entry.name.casefold()
                                if name in (
                                    GLOBAL_SKIP_DIRS
                                    | software_catalog.INTERNAL_DIRECTORIES
                                    | {"dotnet"}
                                ) or (at_root and name in ROOT_SKIP_DIRS):
                                    stats.skipped_system += 1
                                    continue
                                stack.append((entry.path, False))
                            elif stat.S_ISREG(info.st_mode) and not helper_executable(entry.path):
                                stats.executables += 1
                                if stats.executables > candidate_limit:
                                    stats.limit_reached = True
                                    if progress:
                                        progress(asdict(stats), entry.path)
                                    return
                                yield {
                                    "name": os.path.splitext(entry.name)[0],
                                    "path": entry.path,
                                    "source": "filesystem",
                                    "scan_root": root,
                                }
                        except OSError:
                            stats.inaccessible += 1
                        now = time.monotonic()
                        if progress and now - last_notice >= 0.12:
                            progress(asdict(stats), directory)
                            last_notice = now
            except OSError:
                stats.inaccessible += 1
            if progress and time.monotonic() - last_notice >= 0.12:
                progress(asdict(stats), directory)
                last_notice = time.monotonic()
        stats.roots_done += 1
        if progress:
            progress(asdict(stats), root)


def read_executable_identity(path):
    path = clean_path(path)
    if os.name != "nt" or not path:
        return {}
    try:
        info = os.stat(path)
    except OSError:
        return {}
    return dict(_version_identity(path, info.st_mtime_ns, info.st_size))


@lru_cache(maxsize=4096)
def _version_identity(path, modified, size_on_disk):
    del modified, size_on_disk
    result = {}
    try:
        api = ctypes.WinDLL("version", use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        api.GetFileVersionInfoW.restype = wintypes.BOOL
        api.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        api.VerQueryValueW.restype = wintypes.BOOL
        ignored = wintypes.DWORD()
        size = api.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
        if not size or size > 4 * 1024 * 1024:
            return {}
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(path, 0, size, buffer):
            return {}
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        translations = []
        if (
            api.VerQueryValueW(
                buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)
            )
            and pointer.value
        ):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
            for index in range(0, min(length.value // 2 - 1, 128), 2):
                translations.append((words[index], words[index + 1]))
        translations.extend(((0x409, 0x4B0), (0x409, 0x4E4)))
        fields = {
            "CompanyName": "publisher",
            "ProductName": "product",
            "FileDescription": "description",
            "OriginalFilename": "original_filename",
            "InternalName": "internal_name",
        }
        for language, codepage in dict.fromkeys(translations):
            for field, target in fields.items():
                if target in result:
                    continue
                pointer, length = ctypes.c_void_p(), wintypes.UINT()
                query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
                if (
                    api.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length))
                    and pointer.value
                    and 0 < length.value < 65536
                ):
                    value = ctypes.wstring_at(pointer, length.value).rstrip("\x00").strip()
                    if value:
                        result[target] = value[:512]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return result


@lru_cache(maxsize=256)
def _directory_evidence(directory, modified):
    del modified
    try:
        with os.scandir(directory) as entries:
            names = set()
            for index, entry in enumerate(entries):
                if index >= 4096:
                    break
                name = entry.name.casefold()
                if (
                    name
                    in {"unityplayer.dll", "gameassembly.dll", "steam_api.dll", "steam_api64.dll"}
                    or name.endswith(("_data", ".pck"))
                    or name.startswith("goggame-")
                ):
                    names.add(name)
        return frozenset(names)
    except OSError:
        return frozenset()


def game_evidence(path):
    directory = os.path.dirname(path)
    try:
        info = os.stat(directory)
        modified = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_nlink)
    except OSError:
        return []
    names = _directory_evidence(directory, modified)
    stem = os.path.splitext(os.path.basename(path))[0].casefold()
    evidence = []
    if "unityplayer.dll" in names and (stem + "_data" in names or "gameassembly.dll" in names):
        evidence.append("Unity player with game data")
    if "steam_api64.dll" in names or "steam_api.dll" in names:
        evidence.append("Steam game runtime")
    if any(name.startswith("goggame-") and name.endswith((".dll", ".info")) for name in names):
        evidence.append("GOG game runtime")
    if stem + ".pck" in names:
        evidence.append("Matching Godot game package")
    normalized = path.replace("/", "\\").casefold()
    if "\\binaries\\win64\\" in normalized and stem.endswith("-shipping"):
        evidence.append("Unreal shipping executable")
    return evidence


def classify_item(item, *, metadata_reader=read_executable_identity):
    enriched = dict(item)
    path = clean_path(item.get("path", ""))
    enriched["path"] = path
    identity = models.normalize_identity(item.get("identity"))
    for field in ("publisher", "product", "description", "original_filename", "internal_name"):
        if item.get(field):
            identity[field] = str(item[field])[:512]
    if software_catalog.is_runtime_component(item.get("name"), path):
        identity.update(
            kind="ignore", confidence=0.99, classification_reason="runtime/dependency executable"
        )
        enriched.update(
            identity=identity,
            kind="ignore",
            confidence=0.99,
            classification_reasons=["runtime/dependency executable"],
        )
        return enriched
    metadata = metadata_reader(path) or {}
    identity.update({key: str(value)[:512] for key, value in metadata.items() if value})
    source = str(item.get("source") or identity.get("source") or "")
    identity["source"] = source
    if item.get("start_menu_group"):
        identity["start_menu_group"] = str(item["start_menu_group"])[:512]
    basename = ntpath.basename(path).casefold()
    normalized_path = path.replace("/", "\\").casefold()
    product = normalized_text(identity.get("product"), identity.get("description"))
    title = normalized_text(item.get("name"))
    publisher = normalized_text(identity.get("publisher"))
    evidence = game_evidence(path)
    reasons = []
    kind, confidence = "unknown", 0.45
    installed = "registry" in source or "start_menu" in source
    known_client = basename in KNOWN_APP_EXES or any(
        normalized_text(value).strip() == title.strip() for value in KNOWN_LAUNCHER_MARKERS
    )
    game_path = any(marker in normalized_path for marker in GAME_PATH_MARKERS)
    game_publisher = any(marker in publisher for marker in GAME_PUBLISHER_MARKERS)
    app_terms = any(marker in product for marker in APP_MARKERS)
    if software_catalog.is_runtime_component(
        identity.get("product"), path
    ) or software_catalog.embedded_seven_zip({**enriched, "identity": identity}):
        kind, confidence, reasons = "ignore", 0.99, ["runtime or embedded command-line helper"]
    elif ntpath.splitext(path)[1].casefold() not in {".exe", ".bat"}:
        kind, confidence, reasons = "ignore", 1.0, ["unsupported executable type"]
    elif helper_executable(path):
        kind, confidence, reasons = "ignore", 0.99, ["known installer, helper or uninstaller"]
    elif (
        any(
            marker in product
            for marker in (
                " installer ",
                " uninstaller ",
                " update service ",
                " crash reporter ",
                " crashpad handler ",
            )
        )
        and not known_client
    ):
        kind, confidence, reasons = "ignore", 0.98, ["installer/helper version description"]
    elif known_client:
        kind, confidence, reasons = "app", 0.99, ["known application/editor/platform client"]
    elif any(marker in normalized_path for marker in SYSTEM_PATH_MARKERS):
        kind, confidence, reasons = "system", 0.99, ["Windows system executable"]
    elif any(marker in product for marker in DRIVER_MARKERS) and not evidence:
        kind, confidence, reasons = "driver", 0.98, ["driver/firmware description"]
    elif evidence:
        kind, confidence, reasons = "game", 0.96, evidence
    elif game_path:
        kind, confidence, reasons = "game", 0.93, ["game-library directory; helper filters passed"]
    elif game_publisher and (" game " in product or " gameplay " in product):
        kind, confidence, reasons = (
            "game",
            0.90,
            ["game publisher and game-specific product description"],
        )
    elif installed and not game_publisher:
        kind, confidence, reasons = "app", 0.92, ["registered/Start Menu user application"]
    elif app_terms and identity.get("publisher") and not game_publisher:
        kind, confidence, reasons = "app", 0.90, ["application product metadata and publisher"]
    else:
        reasons = ["insufficient evidence; keep for manual review"]
        if game_publisher:
            reasons.append("publisher alone does not prove this is a game")
    if metadata.get("product") and source == "filesystem":
        enriched["name"] = metadata["product"][:256]
    identity["kind"], identity["confidence"] = kind, confidence
    identity["classification_reason"] = "; ".join(reasons)[:512]
    enriched.update(
        identity=identity, kind=kind, confidence=confidence, classification_reasons=reasons
    )
    return enriched


def reset_caches():
    """Each explicit scan rechecks current files, even on coarse-timestamp volumes."""
    _version_identity.cache_clear()
    _local_drive_type.cache_clear()
    _directory_evidence.cache_clear()
