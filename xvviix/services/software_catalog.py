"""Conservative user-application filtering and duplicate selection for local discovery."""

import ntpath
import os
import re

from ..utils import canonical_path, clean_path

RUNTIME_EXES = frozenset(
    {
        "dotnet.exe",
        "apphost.exe",
        "msbuild.exe",
        "csc.exe",
        "vbc.exe",
        "ngen.exe",
        "aspnet_regiis.exe",
        "vstest.console.exe",
        "7z.exe",
        "7za.exe",
        "7zr.exe",
        "7zg.exe",
    }
)
INTERNAL_DIRECTORIES = frozenset(
    {
        "shared",
        "sdk",
        "packs",
        "host",
        "redist",
        "redistributable",
        "runtimes",
        "packages",
        "resources",
        "plugins",
        "node_modules",
        "__pycache__",
        ".git",
        ".cache",
        "cache",
    }
)


def normalized_name(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def is_runtime_component(name, path=""):
    base = ntpath.basename(clean_path(path)).casefold()
    if base in RUNTIME_EXES:
        return True
    text = normalized_name(name)
    runtime_patterns = (
        r"\b(?:microsoft )?(?:asp net|net|dotnet)(?: core)? (?:host|runtime|sdk|targeting|apphost|framework|desktop|standard|native)",
        r"\bmicrosoft windows desktop runtime\b",
        r"\bmicrosoft visual c\b.*\b(?:redistributable|runtime|minimum|additional)\b",
        r"\bmicrosoft edge webview2 runtime\b",
        r"\b(?:windows software development kit|windows sdk|directx runtime)\b",
    )
    return any(re.search(pattern, text) for pattern in runtime_patterns)


def is_seven_zip(name):
    compact = re.sub(r"[^a-z0-9]", "", str(name or "").casefold())
    return compact.startswith("7zip") or compact in {"7zfm", "7zfmexe"}


def installation_anchor(item):
    path = clean_path(item.get("path", ""))
    explicit = clean_path(item.get("install_location") or item.get("install_root") or "")
    if explicit:
        return canonical_path(explicit)
    search_root = clean_path(item.get("scan_root") or "")
    if search_root:
        relative = ntpath.relpath(path, search_root)
        parts = relative.replace("/", "\\").split("\\")
        if len(parts) > 1 and not relative.startswith(".."):
            return canonical_path(ntpath.join(search_root, parts[0]))
    return canonical_path(ntpath.dirname(path))


def embedded_seven_zip(item):
    path = clean_path(item.get("path", ""))
    base = ntpath.basename(path).casefold()
    if base in {"7z.exe", "7za.exe", "7zr.exe", "7zg.exe"}:
        return True
    if base != "7zfm.exe":
        return False
    identity = item.get("identity") or {}
    source = str(item.get("source") or identity.get("source") or "")
    # An actual registered 7-Zip GUI entry is allowed. A copy embedded in another
    # product's tools/resources directory is not a separate application.
    registered_name = item.get("registered_name") or item.get("product") or item.get("name")
    if "registry" in source and is_seven_zip(registered_name):
        return False
    return not is_seven_zip(ntpath.basename(installation_anchor(item)))


def preferred_registered_executable(name, target, install_location):
    if not is_seven_zip(name):
        return target
    directories = [clean_path(install_location), ntpath.dirname(clean_path(target))]
    for directory in directories:
        if directory:
            candidate = os.path.join(directory, "7zFM.exe")
            if os.path.isfile(candidate):
                return candidate
    return target


def known_installation_roots(records=(), *, environ=None):
    env = os.environ if environ is None else environ
    roots = []
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if env.get(key):
            roots.append(clean_path(env[key]))
    if env.get("LOCALAPPDATA"):
        roots.append(os.path.join(env["LOCALAPPDATA"], "Programs"))
    for item in records:
        if is_runtime_component(item.get("name"), item.get("path")):
            continue
        location = clean_path(item.get("install_location") or ntpath.dirname(item.get("path", "")))
        if location:
            roots.append(location)
    valid = []
    for path in sorted(set(roots), key=len):
        key = canonical_path(path)
        drive, tail = ntpath.splitdrive(key)
        if not key or (drive and not tail.strip("\\/")) or key in {"/", "\\"}:
            continue
        if key.startswith("\\\\") or not os.path.isdir(path):
            continue
        if any(key == prior or key.startswith(prior.rstrip("\\/") + "\\") for _p, prior in valid):
            continue
        valid.append((path, key))
    return [path for path, _key in valid]


def _family(item):
    identity = item.get("identity") or {}
    product = identity.get("product") or item.get("product") or item.get("name")
    if is_seven_zip(product):
        return "7zip"
    normalized = normalized_name(product)
    return re.sub(r"\b(?:x64|x86|64 bit|32 bit|\d+(?: \d+)+)\b", "", normalized).strip()


def _rank(item):
    path = clean_path(item.get("path", ""))
    base = ntpath.splitext(ntpath.basename(path))[0].casefold()
    identity = item.get("identity") or {}
    source = str(item.get("source") or identity.get("source") or "")
    family = _family(item).replace(" ", "")
    score = 100 if "registry" in source else (55 if "start_menu" in source else 0)
    if base == "7zfm":
        score += 150
    if base.replace(" ", "") == family:
        score += 55
    anchor = installation_anchor(item)
    depth = len(ntpath.relpath(path, anchor).replace("/", "\\").split("\\")) if anchor else 9
    return (score - depth * 3, -len(path))


def deduplicate_applications(items):
    """Pick one main executable per product/install family; never alter existing libraries."""
    grouped, output = {}, []
    for item in items:
        if item.get("kind") in {"ignore", "system", "driver"}:
            output.append(item)
            continue
        if is_runtime_component(item.get("name"), item.get("path")) or embedded_seven_zip(item):
            output.append(
                {
                    **item,
                    "kind": "ignore",
                    "confidence": 0.99,
                    "classification_reasons": ["runtime or embedded helper"],
                }
            )
            continue
        identity = item.get("identity") or {}
        family = _family(item)
        anchor = installation_anchor(item)
        publisher = normalized_name(identity.get("publisher") or item.get("publisher"))
        key = (
            (family, publisher, anchor)
            if family and anchor
            else (canonical_path(item.get("path", "")),)
        )
        previous = grouped.get(key)
        if previous is None or _rank(item) > _rank(previous):
            grouped[key] = item
    return output + list(grouped.values())
