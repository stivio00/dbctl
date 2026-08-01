"""Append-only JSONL audit log at ~/.dbctl/history.jsonl."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from dbctl.config import history_path


def append(
    *,
    profile: str | None,
    connection: str,
    operation: str | None,
    params: dict | None,
    mode: str,
    status: str,
    rows_affected: int | None = None,
    duration_ms: float = 0.0,
    actor: str | None = None,
    redact: set[str] | None = None,
) -> str:
    entry = {
        "run_id": uuid.uuid4().hex[:12],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "connection": connection,
        "operation": operation,
        "mode": mode,
        "params": _redact(params or {}, redact or set()),
        "status": status,
        "rows_affected": rows_affected,
        "duration_ms": round(duration_ms, 1),
        "actor": actor,
    }
    path: Path = history_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry["run_id"]


def _redact(params: dict, secret_names: set[str]) -> dict:
    return {k: ("***" if k in secret_names else v) for k, v in params.items()}


def read(profile: str | None, *, limit: int = 50) -> list[dict]:
    """Return the last ``limit`` parseable entries, oldest→newest.

    We parse *every* line and only truncate at the end: a half-written tail
    line (crash mid-append) must not evict a valid older entry from the
    window the way ``lines[-limit:]`` would have.
    """
    path = history_path(profile)
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-limit:] if limit > 0 else entries


def last_for(profile: str | None, connection: str) -> dict | None:
    for entry in reversed(read(profile, limit=200)):
        if entry.get("connection") == connection and entry.get("operation"):
            return entry
    return None
