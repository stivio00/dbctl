"""AWS SSM port-forward tunnel via the ``aws`` CLI subprocess."""

from __future__ import annotations

import atexit
import json
import subprocess

from rich.console import Console

from dbctl.config import SsmTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open

_console = Console(stderr=True)


def _resolve_bastion_id(conn: _Conn) -> str:
    """Return the SSM target. If ``bastion_instance_id`` is set use it
    directly; otherwise resolve ``bastion_tags`` via ``aws ec2
    describe-instances`` (so users can rotate bastions without editing YAML).

    Always filters for ``instance-state-name=running`` so a recently-replaced
    or terminated bastion is never selected. Warns (to stderr) when more than
    one instance matches the tags — picks the first, but the operator should
    tighten the tag set.
    """
    if conn.bastion_instance_id:
        return conn.bastion_instance_id
    if not conn.bastion_tags:
        raise RuntimeError("ssm tunnel: neither 'bastion_instance_id' nor 'bastion_tags' is set")

    # Use the JSON filter form so multi-tag filters are never mis-tokenised by
    # the AWS CLI's shorthand parser. Always add instance-state-name=running
    # so we don't pick up a terminated / stopping bastion.
    tag_filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in conn.bastion_tags.items()]
    tag_filters.append({"Name": "instance-state-name", "Values": ["running"]})

    cmd = [
        "aws",
        "ec2",
        "describe-instances",
        "--region",
        conn.region,
        "--filters",
        json.dumps(tag_filters),
        "--query",
        "Reservations[].Instances[].[InstanceId] | []",
        "--output",
        "text",
    ]
    if conn.profile:
        cmd[1:1] = ["--profile", conn.profile]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as e:
        raise RuntimeError(
            "the `aws` CLI was not found on PATH — install it (e.g. "
            "`pip install awscli` or your OS package) and re-authenticate"
        ) from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"failed to resolve bastion tags {conn.bastion_tags}: {e.stderr.strip()[:200]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"aws ec2 describe-instances timed out resolving {conn.bastion_tags}") from e

    instance_ids = [line for line in result.stdout.splitlines() if line.strip()]
    if not instance_ids:
        raise RuntimeError(f"no running instance found matching tags {conn.bastion_tags}")
    if len(instance_ids) > 1:
        _console.print(
            f"[yellow]warning:[/yellow] {len(instance_ids)} instances match tags "
            f"{conn.bastion_tags}: {instance_ids}. "
            f"Using {instance_ids[0]!r} — add more tags to disambiguate."
        )
    return instance_ids[0]


class SsmTunnel:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.local_host = "127.0.0.1"
        self.local_port = conn.local_port or find_free_port()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> int:
        target = _resolve_bastion_id(self.conn)
        params = {
            "host": [self.conn.remote_host],
            "portNumber": [str(self.conn.remote_port)],
            "localPortNumber": [str(self.local_port)],
        }
        cmd = [
            "aws",
            "ssm",
            "start-session",
            "--region",
            self.conn.region,
            "--target",
            target,
            "--document-name",
            self.conn.ssm_document,
            "--parameters",
            json.dumps(params),
        ]
        if self.conn.profile:
            cmd[1:1] = ["--profile", self.conn.profile]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "the `aws` CLI was not found on PATH — install it before opening an SSM tunnel"
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
                f"SSM tunnel did not come up on 127.0.0.1:{self.local_port}. "
                f"aws stderr: {stderr.strip()[:500]}"
            )
        return self.local_port

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None
