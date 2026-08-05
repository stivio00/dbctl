"""GCP Identity-Aware Proxy (IAP) TCP tunnel via the ``gcloud`` CLI subprocess."""

from __future__ import annotations

import atexit
import shutil
import subprocess

from dbctl.config import GcpIapTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open


class GcpIapTunnel:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.local_host = "127.0.0.1"
        self.local_port = conn.local_port or find_free_port()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> int:
        # On Windows the Google Cloud SDK installs as `gcloud.cmd`, not
        # `gcloud.exe` — subprocess.Popen(["gcloud", ...]) without
        # shell=True raises FileNotFoundError even though `gcloud` is
        # genuinely on PATH, because CreateProcess won't resolve a bare
        # command name to a .cmd/.bat launcher. Resolving the full path
        # via shutil.which first fixes this on Windows and is a no-op on
        # Linux/macOS (real executable).
        cmd = [
            shutil.which("gcloud") or "gcloud",
            "compute",
            "start-iap-tunnel",
            self.conn.instance,
            str(self.conn.remote_port),
            f"--local-host-port=localhost:{self.local_port}",
            "--zone",
            self.conn.zone,
        ]
        if self.conn.project:
            cmd.append(f"--project={self.conn.project}")

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "the `gcloud` CLI was not found on PATH — install the Google Cloud "
                "CLI (https://cloud.google.com/sdk/docs/install) before opening a "
                "gcp tunnel"
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
                f"GCP IAP tunnel did not come up on 127.0.0.1:{self.local_port} "
                f"(instance={self.conn.instance}, zone={self.conn.zone}). "
                f"gcloud stderr: {stderr.strip()[:500]}"
            )
        return self.local_port

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None
