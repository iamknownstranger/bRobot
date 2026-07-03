"""sources.yaml loading + schedule parsing, shared by worker and critic."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from freebie_agent.models import SourceStatus

_SCHEDULE_RE = re.compile(r"^(\d+)\s*([mhd])$")
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


def parse_schedule_minutes(schedule: str, default_minutes: int) -> int:
    """'30m' / '2h' / '1d' -> minutes; empty string -> the config default."""
    schedule = schedule.strip().lower()
    if not schedule:
        return default_minutes
    m = _SCHEDULE_RE.match(schedule)
    if not m:
        raise ValueError(f"invalid schedule {schedule!r} (expected e.g. '30m', '2h', '1d')")
    return int(m.group(1)) * _UNIT_MINUTES[m.group(2)]


def load_sources(path: Path) -> list[dict[str, Any]]:
    """Parse and validate sources.yaml. Fail fast on malformed entries."""
    if not path.is_file():
        raise FileNotFoundError(f"sources file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("sources")
    if not isinstance(entries, list):
        raise ValueError("sources.yaml: top-level 'sources' list is required")
    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry or "type" not in entry:
            raise ValueError(f"sources.yaml: entry needs at least id and type: {entry!r}")
        source_id = str(entry["id"])
        if source_id in seen_ids:
            raise ValueError(f"sources.yaml: duplicate source id {source_id!r}")
        seen_ids.add(source_id)
        status = str(entry.get("status", "active"))
        if status not in {s.value for s in SourceStatus}:
            raise ValueError(f"sources.yaml: source {source_id!r} has invalid status {status!r}")
        result.append(dict(entry))
    return result
