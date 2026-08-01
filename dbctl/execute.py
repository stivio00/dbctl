"""Translate ``$name`` placeholders to SQLAlchemy ``:name`` and execute."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.engine import Connection as SAConnection
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from dbctl.config import Operation

_PLACEHOLDER = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")


def to_bindparams(sql: str) -> str:
    """Rewrite ``$name`` → ``:name`` while leaving everything else (literals,
    operators, dollar-quoting edge cases) untouched.

    We deliberately do **not** touch ``$$ ... $$`` dollar-quoted strings: those
    are matched only when the char after ``$`` is a digit (which is an invalid
    bindparam name anyway), so they survive intact.
    """
    return _PLACEHOLDER.sub(r":\1", sql)


@dataclass
class ExecResult:
    rows_affected: int | None
    rows: list[dict] | None
    latency_ms: float
    message: str = ""


def _coerce(value: Any, ptype: str) -> Any:
    if value is None:
        return None
    match ptype:
        case "integer":
            return int(value)
        case "float":
            return float(value)
        case "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() in {"1", "true", "yes", "on"}
        case "list":
            if isinstance(value, (list, tuple)):
                return list(value)
            return [v.strip() for v in str(value).split(",") if v.strip()]
        case "path":
            return str(value)
        case "date":
            return str(value)  # let the driver parse
        case "secret" | "string" | _:
            return str(value)


def bind_params(op: Operation, params: dict[str, Any]) -> dict[str, Any]:
    """Coerce each provided value to the declared type and fill defaults."""
    out: dict[str, Any] = {}
    for p in op.parameters:
        if p.name in params and params[p.name] is not None:
            out[p.name] = _coerce(params[p.name], p.type.value)
        elif p.default is not None:
            out[p.name] = _coerce(p.default, p.type.value)
        elif p.required:
            raise ValueError(f"missing required parameter: --{p.name}")
        # else: leave unset → SQL uses NULL if bound later; or it's optional.
    return out


def _render(conn: SAConnection, op: Operation, params: dict[str, Any]) -> ExecResult:
    started = time.monotonic()
    try:
        if op.mode.value in {"execute", "fetch", "fetch_one", "script"}:
            stmt = text(to_bindparams(op.sql or ""))
            if op.mode.value == "fetch_one":
                row = conn.execute(stmt, params).mappings().first()
                rows = [dict(row)] if row else []
                return ExecResult(1, rows, (time.monotonic() - started) * 1000)
            result = conn.execute(stmt, params)
            if op.mode.value == "fetch":
                rows = [dict(r) for r in result.mappings()]
                return ExecResult(result.rowcount, rows, (time.monotonic() - started) * 1000)
            return ExecResult(result.rowcount, None, (time.monotonic() - started) * 1000)
        if op.mode.value == "upsert":
            # Build a dialect-aware upsert dynamically. Feeds off a YAML
            # fixture file: see `upsert.py` for the heavy lifting.
            raise RuntimeError("upsert mode is dispatched in execute.upsert, not here")
        raise NotImplementedError(f"mode {op.mode!r} not supported in single-conn execute")
    except SQLAlchemyError as e:
        orig = getattr(e, "orig", None)
        detail = str(orig) if orig is not None else str(e)
        raise RuntimeError(f"SQL failed: {e.__class__.__name__}: {detail}") from e


def render(conn: SAConnection, op: Operation, params: dict[str, Any]) -> ExecResult:
    """Execute a single-connection operation. Caller owns the transaction."""
    return _render(conn, op, params)


def format_sql(op: Operation, params: dict[str, Any]) -> str:
    """Show the SQL as it would be sent, with bound values interpolated
    inline — useful for ``--dry-run`` previews and audit logs."""
    sql = op.sql or ""
    pretty = to_bindparams(sql)
    for k, v in params.items():
        pretty = pretty.replace(f":{k}", repr(v))
    return pretty.strip()


__all__ = ["to_bindparams", "bind_params", "render", "format_sql", "ExecResult"]