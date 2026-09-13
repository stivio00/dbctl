"""Secret-free summaries of the two registries.

Shared by the AI-facing surfaces — ``dbctl context``, ``dbctl ask`` and
``dbctl mcp`` — so an LLM (local or remote) always sees the same shape:
one dict per connection / operation, with credentials and full URLs
stripped. Passwords, ``password_env`` names and ``url:`` strings never
appear in a catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbctl.config import Connection, Operation


def connection_summary(name: str, conn: Connection) -> dict[str, Any]:
    """Metadata-only view of a connection (no secrets)."""
    return {
        "name": name,
        "description": conn.description,
        "aliases": list(conn.aliases),
        "type": conn.type.value,
        "driver": conn.driver or ("(from url)" if conn.url else ""),
        "database": conn.database or "",
        "read_only": conn.safety.read_only,
        "allowed_operations": list(conn.safety.allowed_operations),
    }


def connections_catalog(conns: dict[str, Connection]) -> list[dict[str, Any]]:
    return [connection_summary(n, conns[n]) for n in sorted(conns)]


def operation_summary(name: str, op: Operation, *, include_sql: bool = False) -> dict[str, Any]:
    """Metadata view of an operation; ``sql`` is opt-in because the
    catalog is also handed to third-party LLM APIs."""
    out: dict[str, Any] = {
        "name": name,
        "description": op.description,
        "scope": op.scope.value,
        "mode": op.mode.value,
        "parameters": [
            {
                "name": p.name,
                "type": p.type.value,
                "required": p.required,
                "default": p.default,
                "description": p.description,
                "choices": p.choices,
                "position": p.position,
                "secret": p.type.value == "secret",
            }
            for p in op.parameters
        ],
    }
    if op.tags:
        out["tags"] = list(op.tags)
    if include_sql and op.sql:
        out["sql"] = op.sql.strip()
    return out


def operations_catalog(ops: dict[str, Operation], *, include_sql: bool = False) -> list[dict[str, Any]]:
    return [operation_summary(n, ops[n], include_sql=include_sql) for n in sorted(ops)]


def redact_params(op: Operation, params: dict[str, Any]) -> dict[str, Any]:
    """Drop params whose declared type is ``secret`` from a plan/preview
    that may be shown to an LLM. Non-secret values pass through."""
    secrets = {p.name for p in op.parameters if p.type.value == "secret"}
    return {k: ("***" if k in secrets else v) for k, v in params.items()}


__all__ = [
    "connection_summary",
    "connections_catalog",
    "operation_summary",
    "operations_catalog",
    "redact_params",
]
