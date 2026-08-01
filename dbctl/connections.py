"""Load and resolve the connections registry."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import yaml
from rich.console import Console

from dbctl.config import Connection, ConnectionsFile, connections_path

_console = Console()


class UnknownConnectionError(KeyError):
    def __init__(self, name: str, available: list[str]):
        super().__init__(name)
        self.name = name
        self.available = available

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        import difflib

        sug = difflib.get_close_matches(self.name, self.available, n=1)
        hint = f"  did you mean {sug[0]!r}?" if sug else ""
        return f"unknown connection {self.name!r}.{hint}"


def load(path: Path | None = None, profile: str | None = None) -> dict[str, Connection]:
    p = path or connections_path(profile)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    return ConnectionsFile.model_validate(raw).connections


def index(reg: dict[str, Connection]) -> dict[str, tuple[str, Connection]]:
    """Map every alias + canonical name back to (canonical, conn)."""
    out: dict[str, tuple[str, Connection]] = {}
    for name, conn in reg.items():
        out[name] = (name, conn)
        for alias in conn.aliases:
            out[alias] = (name, conn)
    return out


def resolve(name: str, reg: dict[str, Connection]) -> tuple[str, Connection] | NoReturn:
    ix = index(reg)
    if name in ix:
        return ix[name]
    raise UnknownConnectionError(name, list(reg.keys()))