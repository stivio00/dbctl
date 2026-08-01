"""Load and validate the operations registry."""

from __future__ import annotations

import difflib
from pathlib import Path

import yaml

from dbctl.config import Operation, OperationsFile, operations_path


class UnknownOperationError(KeyError):
    def __init__(self, name: str, available: list[str]):
        super().__init__(name)
        self.name = name
        self.available = available

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        sug = difflib.get_close_matches(self.name, self.available, n=1)
        hint = f"  did you mean {sug[0]!r}?" if sug else ""
        return f"unknown operation {self.name!r}.{hint}"


def load(path: Path | None = None, profile: str | None = None) -> dict[str, Operation]:
    p = path or operations_path(profile)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    return OperationsFile.model_validate(raw).operations


def resolve(name: str, reg: dict[str, Operation]):
    if name in reg:
        return reg[name]
    raise UnknownOperationError(name, list(reg.keys()))


def by_scope(reg: dict[str, Operation], scope: str) -> dict[str, Operation]:
    return {n: o for n, o in reg.items() if o.scope.value == scope}
