"""Load and validate the operations registry."""

from __future__ import annotations

import difflib
from pathlib import Path

import yaml

from dbctl.config import Operation, operations_path


class UnknownOperationError(KeyError):
    def __init__(self, name: str, available: list[str]):
        super().__init__(name)
        self.name = name
        self.available = available

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        sug = difflib.get_close_matches(self.name, self.available, n=1)
        hint = f"  did you mean {sug[0]!r}?" if sug else ""
        return f"unknown operation {self.name!r}.{hint}"


class OperationsFileError(Exception):
    """Raised when one or more operations fail to validate.

    Carries both the per-operation error messages (for friendly reporting)
    and the subset of operations that /did/ validate, so the CLI can keep
    serving the good ones instead of refusing to load the whole registry
    because a single operation YAML block is mis-declared. Mirrors
    ``ConnectionsFileError`` so the loader-resilience guarantee holds for
    both registries.
    """

    def __init__(self, errors: dict[str, str], valid: dict[str, Operation] | None = None):
        self.errors = dict(errors)
        self.valid = dict(valid or {})
        super().__init__(self._render())

    def _render(self) -> str:
        n = len(self.errors)
        head = f"{n} invalid operation{'s' if n != 1 else ''} skipped:"
        lines = [head]
        for name, msg in self.errors.items():
            lines.append(f"  {name}: {msg}")
        return "\n".join(lines)


def _first_validation_msg(e: Exception) -> str:
    """Reduce a pydantic ValidationError (often many lines) to one concise
    human-readable sentence: the first leaf error's message, optionally
    prefixed by its field location. Mirrors the connection loader so a
    ``operations.yaml`` typo surfaces as e.g.

        broken-op: copy with table introspection (no `tables:` list) requires roles [src, trg]

    rather than the four-line pydantic dump with the
    ``For further information visit https://errors.pydantic.dev/...`` tail.
    """
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            first = errs()[0]
        except Exception:  # noqa: BLE001
            return str(e).split("\n", 1)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()) if p not in ("__root__", ""))
        msg = first.get("msg", str(e))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        return f"{loc}: {msg}" if loc else msg
    return str(e).split("\n", 1)[0]


def _friendly_yaml_error(e: Exception) -> str:
    """Collapse a ``yaml.YAMLError`` into a one-line friendly message with the
    file's line:column prefix, e.g.

        :3:1: found character '\\t' that cannot start any token

    The caller (``load``) wraps it with the registry name
    (``"connections.yaml: YAML parse error" + …``). Falls back to ``type(e).__name__``
    for empty messages. Multi-line ``scanner`` dumps are flattened to a single
    sentence so a malformed YAML no longer reaches the user as a multi-line
    traceback.
    """
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None) or str(e).split("\n", 1)[0]
    loc = f":{mark.line + 1}:{mark.column + 1}" if mark is not None and (mark.line or mark.column) else ""
    if not problem:
        return type(e).__name__
    return f"{loc}: {problem}".replace("\n", " ").strip()


def load(path: Path | None = None, profile: str | None = None) -> dict[str, Operation]:
    p = path or operations_path(profile)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise OperationsFileError({"<file>": f"YAML parse error{_friendly_yaml_error(e)}"}) from e
    except OSError as e:
        raise OperationsFileError({"<file>": f"read error: {e}"}) from e

    if not isinstance(raw, dict) or "operations" not in raw:
        raise OperationsFileError({"<file>": "missing top-level 'operations:' mapping"})
    ops_raw = raw.get("operations") or {}
    if not isinstance(ops_raw, dict):
        raise OperationsFileError({"<file>": "'operations:' must be a mapping of name -> operation"})

    valid: dict[str, Operation] = {}
    errors: dict[str, str] = {}
    for name, o in ops_raw.items():
        try:
            valid[name] = Operation.model_validate(o)
        except Exception as e:  # noqa: BLE001 - capture per-op failures
            errors[str(name)] = _first_validation_msg(e)
    if errors:
        raise OperationsFileError(errors, valid)
    return valid


def resolve(name: str, reg: dict[str, Operation]):
    if name in reg:
        return reg[name]
    raise UnknownOperationError(name, list(reg.keys()))


def by_scope(reg: dict[str, Operation], scope: str) -> dict[str, Operation]:
    return {n: o for n, o in reg.items() if o.scope.value == scope}
