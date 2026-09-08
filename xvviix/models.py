"""Validation/normalization of persisted records, without GUI or filesystem access."""

import uuid
from .utils import hex_to_rgb, random_color

IDENTITY_TEXT_FIELDS = (
    "publisher",
    "product",
    "description",
    "original_filename",
    "internal_name",
    "source",
    "start_menu_group",
    "kind",
    "classification_reason",
)


def normalize_identity(raw):
    """Keep only bounded scanner identity data that is safe to persist."""
    if not isinstance(raw, dict):
        return {}
    identity = {}
    for field in IDENTITY_TEXT_FIELDS:
        value = str(raw.get(field, "") or "").strip()
        if value:
            identity[field] = value[:512]
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence:
        identity["confidence"] = round(max(0.0, min(1.0, confidence)), 3)
    return identity


def normalize_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("library entries must be JSON objects")
    name = str(raw.get("name", "")).strip()
    path = str(raw.get("path", "")).strip()
    if not name or not path:
        raise ValueError("library entries require a name and path")
    try:
        playtime = max(0, int(raw.get("playtime", 0)))
    except (TypeError, ValueError):
        playtime = 0
    color = str(raw.get("color", ""))
    try:
        hex_to_rgb(color)
    except (TypeError, ValueError):
        color = random_color()
    item = {
        "name": name,
        "path": path,
        "trainer": str(raw.get("trainer", "") or ""),
        "icon": str(raw.get("icon", "") or ""),
        "playtime": playtime,
        "pinned": bool(raw.get("pinned", False)),
        "color": color,
    }
    artwork = str(raw.get("artwork", "") or "").strip()
    if artwork:
        item["artwork"] = artwork[:4096]
    identity = normalize_identity(raw.get("identity"))
    if identity:
        item["identity"] = identity
    return item


def _bounded_record_text(value, limit=1024):
    return str(value or "").strip()[:limit]


def normalize_report_sections(raw_sections):
    if not isinstance(raw_sections, list):
        return []
    sections = []
    for raw_section in raw_sections[:16]:
        if not isinstance(raw_section, dict):
            continue
        items = []
        raw_items = raw_section.get("items", [])
        if isinstance(raw_items, list):
            for raw_item in raw_items[:24]:
                if not isinstance(raw_item, dict):
                    continue
                label = _bounded_record_text(raw_item.get("label"), 128)
                value = _bounded_record_text(raw_item.get("value"), 1024)
                if label and value:
                    items.append(
                        {
                            "label": label,
                            "value": value,
                            "status": _bounded_record_text(raw_item.get("status"), 24).lower()
                            or "info",
                        }
                    )
        title = _bounded_record_text(raw_section.get("title"), 128)
        if title and items:
            sections.append(
                {
                    "title": title,
                    "status": _bounded_record_text(raw_section.get("status"), 24).lower() or "info",
                    "items": items,
                }
            )
    return sections


def normalize_report_findings(raw_findings):
    if not isinstance(raw_findings, list):
        return []
    findings = []
    for raw in raw_findings[:32]:
        if not isinstance(raw, dict):
            continue
        title = _bounded_record_text(raw.get("title"), 256)
        if title:
            findings.append(
                {
                    "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "info",
                    "title": title,
                    "detail": _bounded_record_text(raw.get("detail"), 1024),
                    "action": _bounded_record_text(raw.get("action"), 512),
                }
            )
    return findings


def normalize_report_summary(raw_summary):
    if not isinstance(raw_summary, dict):
        return {}
    summary = {}
    for key, value in list(raw_summary.items())[:24]:
        clean_key = _bounded_record_text(key, 64)
        clean_value = _bounded_record_text(value, 512)
        if clean_key and clean_value:
            summary[clean_key] = clean_value
    return summary


def normalize_report(raw):
    """Normalize system diagnostics and retain old records for storage compatibility."""
    if not isinstance(raw, dict):
        raise ValueError("report entries must be JSON objects")
    report_id = _bounded_record_text(raw.get("id"), 80) or uuid.uuid4().hex
    kind = _bounded_record_text(raw.get("kind"), 64) or "legacy_report"
    timestamp = _bounded_record_text(raw.get("timestamp"), 80)
    try:
        epoch = max(0.0, float(raw.get("epoch", 0.0)))
    except (TypeError, ValueError):
        epoch = 0.0
    try:
        pid = max(0, int(raw.get("pid", 0)))
    except (TypeError, ValueError):
        pid = 0
    try:
        runtime_seconds = max(0, int(raw.get("runtime_seconds", 0)))
    except (TypeError, ValueError):
        runtime_seconds = 0
    exit_code = raw.get("exit_code")
    try:
        exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None
    try:
        health_score = max(0, min(100, int(raw.get("health_score", 0))))
    except (TypeError, ValueError):
        health_score = 0
    try:
        scan_duration_ms = max(0, min(3_600_000, int(raw.get("scan_duration_ms", 0))))
    except (TypeError, ValueError):
        scan_duration_ms = 0
    suggestions = raw.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    return {
        "id": report_id,
        "kind": kind,
        "timestamp": timestamp,
        "epoch": epoch,
        "title": _bounded_record_text(raw.get("title"), 256)
        or ("System diagnostic" if kind == "system_report" else "Archived report"),
        "item_name": _bounded_record_text(raw.get("item_name"), 256),
        "item_path": _bounded_record_text(raw.get("item_path"), 4096),
        "pid": pid,
        "exit_code": exit_code,
        "exit_hex": _bounded_record_text(raw.get("exit_hex"), 32),
        "cause": _bounded_record_text(raw.get("cause"), 512) or "Unknown failure",
        "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "warning",
        "runtime_seconds": runtime_seconds,
        "details": _bounded_record_text(raw.get("details"), 4096),
        "fault_module": _bounded_record_text(raw.get("fault_module"), 512),
        "source": _bounded_record_text(raw.get("source"), 128) or "xvviix_process_monitor",
        "health_score": health_score,
        "scan_duration_ms": scan_duration_ms,
        "summary": normalize_report_summary(raw.get("summary")),
        "sections": normalize_report_sections(raw.get("sections")),
        "findings": normalize_report_findings(raw.get("findings")),
        "suggestions": [
            _bounded_record_text(value, 512)
            for value in suggestions[:8]
            if _bounded_record_text(value, 512)
        ],
    }


def normalize_activity(raw):
    if not isinstance(raw, dict):
        raise ValueError("activity entries must be JSON objects")
    try:
        epoch = max(0.0, float(raw.get("epoch", 0.0)))
    except (TypeError, ValueError):
        epoch = 0.0
    return {
        "id": _bounded_record_text(raw.get("id"), 80) or uuid.uuid4().hex,
        "kind": _bounded_record_text(raw.get("kind"), 64) or "status",
        "timestamp": _bounded_record_text(raw.get("timestamp"), 80),
        "epoch": epoch,
        "title": _bounded_record_text(raw.get("title"), 256) or "Launcher activity",
        "item_name": _bounded_record_text(raw.get("item_name"), 256),
        "item_path": _bounded_record_text(raw.get("item_path"), 4096),
        "detail": _bounded_record_text(raw.get("detail"), 1024),
        "severity": _bounded_record_text(raw.get("severity"), 24).lower() or "info",
    }
