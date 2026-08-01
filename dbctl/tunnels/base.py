"""Tunnel protocol, factory, and helpers shared by all implementations."""

from __future__ import annotations

import socket
import subprocess
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from dbctl.config import Connection


@runtime_checkable
class Tunnel(Protocol):
    """Context manager yielding the *local* port SQLAlchemy should connect to."""

    local_host: str
    local_port: int

    def __enter__(self) -> int: ...
    def __exit__(self, *exc) -> None: ...


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_local_open(port: int, timeout: float = 30.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def build_tunnel(conn: Connection) -> Tunnel:
    from dbctl.tunnels.direct import DirectTunnel as _Direct
    from dbctl.tunnels.ssh import SshTunnel as _Ssh
    from dbctl.tunnels.ssm import SsmTunnel as _Ssm

    match conn.type.value:
        case "ssm":
            assert conn.ssm
            return _Ssm(conn.ssm)
        case "ssh":
            assert conn.ssh
            return _Ssh(conn.ssh)
        case "direct":
            assert conn.direct
            return _Direct(conn.direct)
        case _:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown tunnel type {conn.type!r}")


__all__ = ["Tunnel", "build_tunnel", "find_free_port", "wait_local_open", "_terminate"]