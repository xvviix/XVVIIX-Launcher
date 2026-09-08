"""Preserve native icon resources instead of flattening them to a single frame."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

try:
    from PIL import Image, ImageOps
except (ImportError, OSError):
    Image = ImageOps = None

try:
    from icoextract import IconExtractor
except (ImportError, OSError):
    IconExtractor = None

CACHE_PREFIX = "icon_v2_"


def is_user_icon(filename):
    """Never replace user-supplied/custom icon files during discovery."""
    return bool(filename) and not str(filename).startswith("icon_")


def native_icon_sizes(path):
    if Image is None:
        return []
    with Image.open(path) as image:
        if image.format == "ICO" and hasattr(image, "ico"):
            return sorted(image.ico.sizes(), key=lambda size: (size[0] * size[1], size[0]))
        return [image.size]


def extract_native_icon(path, cache_directory, *, resource_index=0, extractor_factory=None):
    """Write an atomic, reusable multi-resolution ICO cache entry.

    The source program is never executed. When its resources do not contain a
    high-resolution frame, this function does not invent/upscale one.
    """
    if Image is None:
        return ""
    source = os.path.abspath(os.fspath(path))
    if source.lower().endswith(".bat"):
        source = os.environ.get("COMSPEC", "") if os.name == "nt" else ""
    if not source or not os.path.isfile(source):
        return ""
    factory = IconExtractor if extractor_factory is None else extractor_factory
    if not source.lower().endswith(".ico") and factory is None:
        return ""
    signature = os.stat(source)
    token = f"native-ico-v2|{os.path.normcase(source)}|{signature.st_mtime_ns}|{signature.st_size}|{int(resource_index)}"
    filename = CACHE_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()[:28] + ".ico"
    directory = Path(cache_directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    if destination.is_file():
        try:
            if native_icon_sizes(destination):
                return filename
        except (OSError, ValueError):
            pass
    descriptor, temporary = tempfile.mkstemp(prefix=".native-icon-", suffix=".ico", dir=directory)
    os.close(descriptor)
    try:
        if source.lower().endswith(".ico"):
            shutil.copyfile(source, temporary)
        else:
            extractor = factory(source)
            if resource_index < 0:
                extractor.export_icon(temporary, resource_id=abs(resource_index))
            else:
                extractor.export_icon(temporary, num=int(resource_index))
        with Image.open(temporary) as check:
            if check.format != "ICO" or not check.ico.sizes():
                raise ValueError("No usable icon resource")
            # Decode the largest stored frame for validation, without rewriting it.
            largest = max(check.ico.sizes(), key=lambda size: size[0] * size[1])
            check.ico.getimage(largest).load()
        os.replace(temporary, destination)
        return filename
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def load_display_icon(path, size):
    """Prefer an exact native frame; otherwise downsample the nearest larger one."""
    if Image is None:
        return None
    size = max(1, int(size))
    with Image.open(path) as opened:
        if opened.format == "ICO" and hasattr(opened, "ico"):
            sizes = sorted(opened.ico.sizes(), key=lambda value: value[0] * value[1])
            suitable = [value for value in sizes if min(value) >= size]
            chosen = suitable[0] if suitable else sizes[-1]
            image = opened.ico.getimage(chosen).convert("RGBA")
        else:
            image = opened.convert("RGBA")
    if image.size != (size, size):
        resized = ImageOps.contain(image, (size, size), Image.Resampling.LANCZOS)
        image.close()
        image = resized
    return image
