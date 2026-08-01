"""Kubernetes port-forward tunnel via the ``kubectl`` CLI subprocess.

Invokes ``kubectl port-forward --context <ctx> [--namespace <ns>] <target>
<local_port>:<remote_port>`` as a subprocess. kubectl writes nothing on
stdout/stderr until the listener is up; wepoll the local port with the
same ``wait_local_open`` helper used by the SSH/SSM tunnels.

If kubectl exits non-zero immediately (e.g. context not found, target
doesn't exist) the wait times out and we surface the captured stderr so
the operator can fix the kubeconfig / context / namespace.
"""

from __future__ import annotations

import atexit
import subprocess

from dbctl.config import K8sTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open


class K8sTunnel:
    """``kubectl port-forward`` subprocess wrapper.

    Reuses the same local_port discovery + atexit cleanup pattern as the
    SSH and SSM tunnels so the lifecycle is identical from the caller's
    perspective.
    """

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.local_host = "127.0.0.1"
        self.local_port = conn.local_port or find_free_port()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> int:
        cmd = [
            "kubectl",
            "port-forward",
            "--context",
            self.conn.context,
        ]
        if self.conn.namespace:
            cmd += ["--namespace", self.conn.namespace]
        cmd += [
            self.conn.target,
            f"{self.local_port}:{self.conn.remote_port}",
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
                "the `kubectl` CLI was not found on PATH — install it "
                "(e.g. via your OS package manager or "
                "https://kubernetes.io/docs/tasks/tools/) before opening a k8s tunnel"
            ) from e
        atexit.register(self._cleanup)

        if not wait_local_open(self.local_port, timeout=30.0):
            stderr = ""
            if self._proc and self._proc.poll() is not None:
                err = self._proc.stderr
                if err:
                    stderr = err.read().decode("utf-8", "replace")
            self._cleanup()
            raise RuntimeError(
                f"kubectl port-forward did not come up on "
                f"127.0.0.1:{self.local_port} (context={self.conn.context}, "
                f"target={self.conn.target}). kubectl stderr: {stderr.strip()[:500]}"
            )
        return self.local_port

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None
