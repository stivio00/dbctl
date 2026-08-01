"""SSH port-forward tunnel via the ``ssh`` CLI subprocess."""

from __future__ import annotations

import atexit
import os
import subprocess

from dbctl.config import SshTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open


class SshTunnel:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.local_host = "127.0.0.1"
        self.local_port = conn.local_port or find_free_port()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> int:
        forward = f"{self.local_port}:{self.conn.remote_host}:{self.conn.remote_port}"
        target = f"{self.conn.user}@{self.conn.host}"

        cmd = [
            "ssh",
            "-N",  # no command, just forwarding
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.conn.port),
            "-i",
            os.path.expanduser(self.conn.identity),
            "-L",
            forward,
            target,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "the `ssh` CLI was not found on PATH — install OpenSSH before opening an SSH tunnel"
            ) from e
        atexit.register(self._cleanup)

        # ssh with ExitOnForwardFailure exits non-zero immediately on port
        # conflict; otherwise the local listener comes up within ~1s.
        if not wait_local_open(self.local_port, timeout=20.0):
            stderr = ""
            if self._proc and self._proc.poll() is not None:
                err = self._proc.stderr
                if err:
                    stderr = err.read().decode("utf-8", "replace")
            self._cleanup()
            raise RuntimeError(
                f"SSH tunnel did not come up on 127.0.0.1:{self.local_port}. "
                f"ssh stderr: {stderr.strip()[:500]}"
            )
        return self.local_port

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None
