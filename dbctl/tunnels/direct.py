"""No-op tunnel for connections reached directly (no port forwarding)."""

from __future__ import annotations

from dataclasses import dataclass

from dbctl.config import DirectTunnel as _Conn


@dataclass
class DirectTunnel:
    """Passthrough: exposes the upstream host/port directly.

    ``local_host``/``local_port`` are the values the SQLAlchemy URL builder
    should use. For direct connections that's the real host/port.
    """

    conn: _Conn
    local_host: str = ""
    local_port: int = 0

    def __enter__(self) -> int:
        self.local_host = self.conn.host
        self.local_port = self.conn.port
        return self.local_port

    def __exit__(self, *exc) -> None:
        return None