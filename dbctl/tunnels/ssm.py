"""AWS SSM port-forward tunnel via the ``aws`` CLI subprocess."""

from __future__ import annotations

import atexit
import configparser
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from dbctl.config import SsmTunnel as _Conn
from dbctl.tunnels.base import _terminate, find_free_port, wait_local_open

_console = Console(stderr=True)


def _aws_config_path() -> Path:
    """Return the AWS CLI config file path, honoring ``AWS_CONFIG_FILE``
    exactly like the ``aws`` CLI itself does."""
    override = os.environ.get("AWS_CONFIG_FILE")
    return Path(override) if override else Path.home() / ".aws" / "config"


def _sso_cache_key(profile: str) -> str | None:
    """Resolve the AWS CLI SSO token-cache *key* for ``profile`` by reading
    ``~/.aws/config`` (or ``$AWS_CONFIG_FILE``).

    The AWS CLI caches SSO tokens under a filename derived from a specific
    string, which depends on the profile's config style:

    * **`sso_session`-based** (the config ``aws configure sso`` now
      generates, and what a separate ``[sso-session <name>]`` block
      implies) — the cache key is the **session name** itself, e.g.
      ``sso_session = my-session`` → key is ``"my-session"``. Note this is
      *not* the session's ``sso_start_url``.
    * **legacy inline SSO** (no ``sso_session``, `sso_start_url` set
      directly on the profile) — the cache key is that ``sso_start_url``.

    Returns ``None`` if the profile (or `default`) isn't found in the
    config file, or has neither ``sso_session`` nor ``sso_start_url`` (e.g.
    a plain access-key profile — not an SSO profile at all).
    """
    config_path = _aws_config_path()
    if not config_path.exists():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path)
    except configparser.Error:
        return None
    section_name = "default" if profile == "default" else f"profile {profile}"
    if section_name not in parser:
        return None
    section = parser[section_name]
    session_name = section.get("sso_session")
    if session_name:
        return session_name
    return section.get("sso_start_url")


def _sso_cache_path(profile: str) -> Path | None:
    """Return the AWS SSO token cache file for the given profile, or
    ``None`` when the profile isn't an SSO profile (or isn't found) — see
    ``_sso_cache_key``.

    AWS caches SSO tokens at ``~/.aws/sso/cache/<sha1(key)>.json`` (SHA-1,
    hex-encoded, of the cache key resolved by ``_sso_cache_key`` — *not* a
    hash of the profile name). Each file contains ``accessToken`` +
    ``expiresAt`` (ISO 8601 UTC).
    """
    key = _sso_cache_key(profile)
    if key is None:
        return None
    hashed = hashlib.sha1(key.encode()).hexdigest()  # AWS CLI's own cache-key scheme
    return Path.home() / ".aws" / "sso" / "cache" / f"{hashed}.json"


def _sso_token_is_valid(profile: str) -> bool:
    """Check whether the SSO token for ``profile`` exists and hasn't expired.

    Returns ``True`` when the token is valid. Returns ``False`` when the
    profile can't be resolved to an SSO cache key, when a token file exists
    but is past its ``expiresAt`` timestamp, or when no token file exists at
    all (which means the user needs to log in).
    """
    cache_file = _sso_cache_path(profile)
    if cache_file is None or not cache_file.exists():
        return False
    try:
        data = json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    expires_str = data.get("expiresAt")
    if not expires_str:
        return False
    # AWS uses ISO 8601 with 'Z' suffix: "2026-08-03T12:34:56Z"
    try:
        expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(UTC)
    # 60-second skade so we don't race the expiry window
    now = now.replace(microsecond=0)
    return expires_dt > now


def _ensure_sso_session(profile: str, *, disable_automatic_sso_login: bool = False) -> None:
    """Ensure the AWS SSO session for ``profile`` is active.

    * If the cached token is valid → return immediately (no-op).
    * If the token is missing or expired and
      ``disable_automatic_sso_login`` is ``False`` → run ``aws sso login
      --profile <profile>`` interactively. This opens the browser for
      SSO authentication; the user clicks through once and the token
      is refreshed in ``~/.aws/sso/cache/``.
    * If the token is missing/expired and
      ``disable_automatic_sso_login`` is ``True`` → raise a clean
      ``RuntimeError`` telling the user to run ``aws sso login`` by
      hand. This is for operators who prefer to manage their SSO
      session separately (e.g. via a shell wrapper or a cron job).
    * If no ``profile`` is set on the connection → return (the user
      is using default credentials, env vars, or an access-key profile;
      we don't interfere).

    This is designed to be **transparent**: on a fresh morning when the
    overnight token expired, ``dbctl`` detects it automatically, triggers
    the browser login, and continues with the tunnel open — no manual
    ``aws sso login`` step needed beforehand.
    """
    if not profile:
        return  # no SSO profile configured; let AWS handle creds its own way

    if _sso_token_is_valid(profile):
        return  # token still good

    if disable_automatic_sso_login:
        raise RuntimeError(
            f"SSO token for profile {profile!r} is missing or expired, and "
            f"'disable_automatic_sso_login: true' is set on this connection. "
            f"Run `aws sso login --profile {profile}` manually, then re-run dbctl."
        )

    _console.print(f"[cyan]SSO token for profile {profile!r} is missing or expired.[/cyan]")
    _console.print(f"[dim]Running `aws sso login --profile {profile}` — a browser window will open...[/dim]")

    cmd = ["aws", "sso", "login", "--profile", profile]
    try:
        result = subprocess.run(cmd, check=False, timeout=300)  # 5 min cap
    except FileNotFoundError as e:
        raise RuntimeError(
            "the `aws` CLI was not found on PATH — install it before opening an SSM tunnel"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("aws sso login timed out (5 min). Re-run when ready.") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"`aws sso login --profile {profile}` failed (exit {result.returncode}). "
            f"Re-authenticate and try again."
        )

    # Verify the token was actually written
    if not _sso_token_is_valid(profile):
        raise RuntimeError(
            f"SSO login completed but no valid token found for profile {profile!r}. "
            f"Check ~/.aws/sso/cache/ or run `aws sso login --profile {profile}` manually."
        )
    _console.print(f"[green]SSO session refreshed for profile {profile!r}.[/green]")


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
        _ensure_sso_session(
            self.conn.profile,
            disable_automatic_sso_login=self.conn.disable_automatic_sso_login,
        )
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
