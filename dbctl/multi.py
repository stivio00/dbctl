"""Multi-connection orchestration: open tunnels, run per-role SQL, join results."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dbctl.db import build_engine
from dbctl.tunnels.base import build_tunnel

if TYPE_CHECKING:
    from dbctl.config import Connection, Operation


@dataclass
class OpenedConn:
    name: str
    engine: object
    tunnel: object
    duration_ms: float = 0.0


@contextmanager
def opened(name: str, conn: Connection) -> Iterator[OpenedConn]:
    tunnel = build_tunnel(conn)
    tunnel.__enter__()
    try:
        engine = build_engine(conn, tunnel)
        yield OpenedConn(name=name, engine=engine, tunnel=tunnel)
    finally:
        tunnel.__exit__(None, None, None)


def run_role(op: Operation, role: str, opened_conn: OpenedConn, params: dict) -> list[dict]:
    """Run one role's query for a multi operation."""
    sql = (op.queries or {}).get(role)
    if sql is None:
        raise RuntimeError(
            f"operation has no query for role {role!r}; "
            f"declared roles: {op.roles}, available: {list((op.queries or {}).keys())}"
        )
    from sqlalchemy import text

    from dbctl.execute import to_bindparams

    started = time.monotonic()
    with opened_conn.engine.connect() as c:
        rows = [dict(r) for r in c.execute(text(to_bindparams(sql)), params).mappings()]
    opened_conn.duration_ms = (time.monotonic() - started) * 1000
    return rows
