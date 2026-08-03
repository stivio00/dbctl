"""Load and resolve the connections registry."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import yaml
from rich.console import Console

from dbctl.config import Connection, connections_path

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


class ConnectionsFileError(Exception):
    """Raised when one or more connections fail to validate.

    Carries both the per-connection error messages (for friendly reporting)
    and the subset of connections that /did/ validate, so the CLI can keep
    serving the good ones instead of refusing to load the whole registry
    because a single reference template is mis-configured.
    """

    def __init__(self, errors: dict[str, str], valid: dict[str, Connection] | None = None):
        self.errors = dict(errors)
        self.valid = dict(valid or {})
        super().__init__(self._render())

    def _render(self) -> str:
        n = len(self.errors)
        head = f"{n} invalid connection{'s' if n != 1 else ''} skipped:"
        lines = [head]
        for name, msg in self.errors.items():
            lines.append(f"  {name}: {msg}")
        return "\n".join(lines)


def _first_validation_msg(e: Exception) -> str:
    """Reduce a pydantic ValidationError (often many lines) to one concise
    human-readable sentence: the first leaf error's message, optionally
    prefixed by its field location.
    """
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            first = errs()[0]
        except Exception:  # noqa: BLE001
            return str(e).split("\n", 1)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()) if p not in ("__root__", ""))
        msg = first.get("msg", str(e))
        # Pydantic prepends "Value error, " / "Input should be ... " etc.
        # Strip the "Value error, " wrapper for model_validator messages.
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        return f"{loc}: {msg}" if loc else msg
    return str(e).split("\n", 1)[0]


def _friendly_yaml_error(e: Exception) -> str:
    """Collapse a ``yaml.scanner.ScannerError`` / ``YAMLError`` into a
    one-line friendly message with the file's line:column prefix, e.g.

        :3:1: while scanning for the next token, found character '\\t' that cannot start any token

    The caller wraps it with the registry name (``"connections.yaml: YAML parse error" + …``).
    Falls back to ``str(e)`` for non-yaml errors and to the type name for
    empty messages. Multi-line ``scanner`` dumps are flattened to a single
    sentence (the line/column helper is preserved) so a malformed YAML no
    longer reaches the user as a four-line traceback.
    """
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None) or str(e).split("\n", 1)[0]
    loc = f":{mark.line + 1}:{mark.column + 1}" if mark is not None and (mark.line or mark.column) else ""
    if not problem:
        return type(e).__name__
    return f"{loc}: {problem}".replace("\n", " ").strip()


def load(path: Path | None = None, profile: str | None = None) -> dict[str, Connection]:
    p = path or connections_path(profile)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConnectionsFileError({"<file>": f"YAML parse error{_friendly_yaml_error(e)}"}) from e
    except OSError as e:
        raise ConnectionsFileError({"<file>": f"read error: {e}"}) from e

    if not isinstance(raw, dict) or "connections" not in raw:
        raise ConnectionsFileError({"<file>": "missing top-level 'connections:' mapping"})
    conns_raw = raw.get("connections") or {}
    if not isinstance(conns_raw, dict):
        raise ConnectionsFileError({"<file>": "'connections:' must be a mapping of name -> connection"})

    valid: dict[str, Connection] = {}
    errors: dict[str, str] = {}
    for name, c in conns_raw.items():
        try:
            valid[name] = Connection.model_validate(c)
        except Exception as e:  # noqa: BLE001 - capture per-connection failures
            errors[str(name)] = _first_validation_msg(e)
    if errors:
        raise ConnectionsFileError(errors, valid)
    return valid


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
