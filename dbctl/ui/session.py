"""Per-connection tunnel+engine lifecycle for the life of the UI app.

``dbctl.runtime.opened_conn`` is a context manager scoped to a single CLI
invocation: open, run once, close. The UI needs a connection to stay open
across many tab-runs while the app is alive, and to be closed explicitly
(disconnect) or when the app exits. That lifecycle lives here - it's the one
piece of state management this UI introduces; everything else composes the
existing ``dbctl.tunnels`` / ``dbctl.db`` helpers.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dbctl.db import DBError, build_engine, healthcheck
from dbctl.tunnels.base import build_tunnel

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from dbctl.config import Connection
    from dbctl.tunnels.base import Tunnel


@dataclass
class ConnectionSession:
    name: str
    conn: Connection
    tunnel: Tunnel | None = None
    engine: Engine | None = None
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.engine is not None


class SessionManager:
    """Keyed by canonical connection name (not aliases)."""

    def __init__(self, connections: dict[str, Connection]) -> None:
        self.connections = connections
        self._sessions: dict[str, ConnectionSession] = {
            name: ConnectionSession(name=name, conn=conn) for name, conn in connections.items()
        }

    def names(self) -> list[str]:
        return list(self._sessions)

    def get(self, name: str) -> ConnectionSession:
        return self._sessions[name]

    def get_or_none(self, name: str) -> ConnectionSession | None:
        """Like `get`, but ``None`` instead of `KeyError` for a connection
        that no longer exists - e.g. an already-open tab whose connection
        was removed from connections.yaml via the tree's edit action."""
        return self._sessions.get(name)

    def connect(self, name: str) -> ConnectionSession:
        session = self._sessions[name]
        if session.connected:
            return session
        session.error = None
        tun = build_tunnel(session.conn)
        try:
            tun.__enter__()
        except RuntimeError as e:
            session.error = f"tunnel error: {e}"
            return session
        try:
            engine = build_engine(session.conn, tun)
            ok, _ms, msg = healthcheck(
                engine, session.conn.healthcheck.query, session.conn.healthcheck.timeout_seconds
            )
        except DBError as e:
            tun.__exit__(None, None, None)
            session.error = f"db error: {e}"
            return session
        if not ok:
            engine.dispose()
            tun.__exit__(None, None, None)
            session.error = f"healthcheck failed: {msg}"
            return session
        session.tunnel = tun
        session.engine = engine
        return session

    def disconnect(self, name: str) -> None:
        session = self._sessions[name]
        if session.engine is not None:
            # engine.dispose() can be called from a different thread than
            # the one that created it (connect() now runs in a worker
            # thread) - some DBAPI drivers (notably sqlite3 with a
            # `:memory:` database) reject that with their own thread-
            # affinity check. Best-effort cleanup: never let it crash the app.
            with contextlib.suppress(Exception):
                session.engine.dispose()
            session.engine = None
        if session.tunnel is not None:
            with contextlib.suppress(Exception):
                session.tunnel.__exit__(None, None, None)
            session.tunnel = None
        session.error = None

    def disconnect_all(self) -> None:
        for name in list(self._sessions):
            self.disconnect(name)

    def test_tunnel(self, name: str) -> tuple[bool, str]:
        """Open + healthcheck + close without touching a live session
        (mirrors ``dbctl tunnel test``)."""
        conn = self.connections[name]
        tun = build_tunnel(conn)
        try:
            tun.__enter__()
        except RuntimeError as e:
            return False, f"tunnel error: {e}"
        try:
            engine = build_engine(conn, tun)
            ok, ms, msg = healthcheck(engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
        except DBError as e:
            return False, f"db error: {e}"
        finally:
            tun.__exit__(None, None, None)
        return (True, f"OK ({ms:.1f}ms)") if ok else (False, msg)

    def reload(self, connections: dict[str, Connection]) -> None:
        """Replace the connection set (e.g. after editing connections.yaml).

        Disconnects everything first. Existing widgets keep their reference
        to this same ``SessionManager`` instance - only its contents change.
        """
        self.disconnect_all()
        self.connections = connections
        self._sessions = {name: ConnectionSession(name=name, conn=conn) for name, conn in connections.items()}
