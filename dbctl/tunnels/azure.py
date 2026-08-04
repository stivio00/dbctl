"""Azure Bastion tunnel via the ``az`` CLI subprocess."""

from __future__ import annotations

import atexit
import shutil
import subprocess

from dbctl.config import AzureBastionTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open


class AzureBastionTunnel:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.local_host = "127.0.0.1"
        self.local_port = conn.local_port or find_free_port()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> int:
        # On Windows the Azure CLI installs as `az.cmd`, not `az.exe` —
        # subprocess.Popen(["az", ...]) without shell=True raises
        # FileNotFoundError even though `az` is genuinely on PATH, because
        # CreateProcess won't resolve a bare command name to a .cmd/.bat
        # launcher. Resolving the full path via shutil.which first fixes
        # this on Windows and is a no-op on Linux/macOS (real executable).
        cmd = [
            shutil.which("az") or "az",
            "network",
            "bastion",
            "tunnel",
            "--name",
            self.conn.bastion_name,
            "--resource-group",
            self.conn.resource_group,
            "--target-resource-id",
            self.conn.target_resource_id,
            "--resource-port",
            str(self.conn.remote_port),
            "--port",
            str(self.local_port),
        ]
        if self.conn.subscription:
            cmd += ["--subscription", self.conn.subscription]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "the `az` CLI was not found on PATH — install the Azure CLI "
                "(https://learn.microsoft.com/cli/azure/install-azure-cli) before "
                "opening an azure tunnel"
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
                f"Azure Bastion tunnel did not come up on 127.0.0.1:{self.local_port} "
                f"(bastion={self.conn.bastion_name}, target={self.conn.target_resource_id}). "
                f"az stderr: {stderr.strip()[:500]}"
            )
        return self.local_port

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None
