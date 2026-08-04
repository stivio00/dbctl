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


def build_tunnel(conn: Connection, *, override_port: int | None = None) -> Tunnel:
    """Construct a Tunnel for the connection's type.

    ``override_port`` lets the CLI (e.g. ``tunnel open --port 1234``) pin
    the local bind port even when the config says ``local_port: 0`` (auto).
    For ``direct`` tunnels the override replaces the upstream port (useful
    for pointing at a different port than the config declares).

    Resolves any ``{{ssm:...}}`` placeholders on ``conn`` first (see
    ``dbctl.refs``) — this is the point a connection is actually used, so
    it's the right place for that lazy resolution to happen.
    """
    from dbctl.refs import resolve_connection
    from dbctl.tunnels.direct import DirectTunnel as _Direct
    from dbctl.tunnels.k8s import K8sTunnel as _K8k
    from dbctl.tunnels.ssh import SshTunnel as _Ssh
    from dbctl.tunnels.ssm import SsmTunnel as _Ssm

    conn = resolve_connection(conn)

    match conn.type.value:
        case "ssm":
            assert conn.ssm
            if override_port is not None:
                # Mutate a copy so the original config is untouched.
                conn.ssm = conn.ssm.model_copy(update={"local_port": override_port})
            return _Ssm(conn.ssm)
        case "ssh":
            assert conn.ssh
            if override_port is not None:
                conn.ssh = conn.ssh.model_copy(update={"local_port": override_port})
            return _Ssh(conn.ssh)
        case "k8s":
            assert conn.k8s
            if override_port is not None:
                conn.k8s = conn.k8s.model_copy(update={"local_port": override_port})
            return _K8k(conn.k8s)
        case "direct":
            assert conn.direct
            if override_port is not None:
                conn.direct = conn.direct.model_copy(update={"port": override_port})
            return _Direct(conn.direct)
        case _:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown tunnel type {conn.type!r}")


__all__ = ["Tunnel", "build_tunnel", "find_free_port", "wait_local_open", "_terminate"]
