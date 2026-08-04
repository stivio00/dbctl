"""Resolve ``{{ssm:...}}`` placeholders in connection values against AWS SSM
Parameter Store, so secrets never need to live in plaintext in
``connections.yaml``.

Reference format::

    {{ssm:<parameter-name>[;property:<json-key>][;profile:<aws-profile>]}}

Resolution shells out to the ``aws`` CLI — the same convention already used
by :mod:`dbctl.tunnels.ssm` for bastion/session lookups — so no extra AWS SDK
dependency is required.

Resolution is deliberately **lazy**: nothing in this module runs at
``connections.yaml`` load time. It only runs when :func:`resolve_connection`
is called, which happens right before a connection is actually used (see
``build_tunnel`` / ``build_engine``). A connection nobody is using — or one
just being displayed via ``dbctl connections show`` — never triggers an AWS
call, so one unreachable profile can't break unrelated connections.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbctl.config import Connection

_REF_RE = re.compile(r"\{\{ssm:([^}]+)\}\}")


class RefResolutionError(RuntimeError):
    """Raised when a ``{{ssm:...}}`` reference cannot be resolved.

    Messages here must only ever name the *reference* (parameter name,
    property, profile) — never the resolved value — so this exception is
    always safe to print to logs/stderr/console.
    """


def _parse_ref(spec: str) -> tuple[str, str | None, str | None]:
    """Split ``<name>[;property:<key>][;profile:<profile>]`` into its parts."""
    parts = spec.split(";")
    name = parts[0].strip()
    if not name:
        raise RefResolutionError(f"ssm reference {{{{ssm:{spec}}}}} is missing a parameter name")

    property_: str | None = None
    profile: str | None = None
    for raw_part in parts[1:]:
        part = raw_part.strip()
        if not part:
            continue
        key, sep, value = part.partition(":")
        if not sep:
            raise RefResolutionError(
                f"ssm reference {{{{ssm:{spec}}}}}: malformed option {part!r} (expected key:value)"
            )
        key = key.strip()
        value = value.strip()
        if key == "property":
            property_ = value
        elif key == "profile":
            profile = value
        else:
            raise RefResolutionError(f"ssm reference {{{{ssm:{spec}}}}}: unknown option {key!r}")
    return name, property_, profile


def _fetch_parameter(name: str, profile: str | None) -> str:
    """Fetch a parameter's raw value via ``aws ssm get-parameter``.

    Always passes ``--with-decryption`` — a no-op for plain ``String``
    parameters, required for ``SecureString`` ones.
    """
    cmd = ["aws", "ssm", "get-parameter", "--name", name, "--with-decryption", "--output", "json"]
    if profile:
        cmd[1:1] = ["--profile", profile]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as e:
        raise RefResolutionError(
            "the `aws` CLI was not found on PATH — install it before resolving ssm: references"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise RefResolutionError(
            f"failed to resolve ssm parameter {name!r}: {stderr[:300] or 'unknown error'}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RefResolutionError(f"aws ssm get-parameter timed out resolving {name!r}") from e

    try:
        payload: Any = json.loads(result.stdout)
        value = payload["Parameter"]["Value"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise RefResolutionError(f"unexpected response resolving ssm parameter {name!r}") from e
    if not isinstance(value, str):
        raise RefResolutionError(f"unexpected response resolving ssm parameter {name!r}")
    return value


def _extract_property(raw_value: str, property_name: str, param_name: str) -> str:
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as e:
        raise RefResolutionError(
            f"ssm parameter {param_name!r} is not valid JSON; cannot extract property {property_name!r}"
        ) from e
    if not isinstance(parsed, dict) or property_name not in parsed:
        raise RefResolutionError(f"ssm parameter {param_name!r} has no property {property_name!r}")
    return str(parsed[property_name])


def resolve_ssm_ref(spec: str, *, cache: dict[str, str]) -> str:
    """Resolve one ``<name>[;property:...][;profile:...]`` reference body
    (the part inside ``{{ssm: }}``), consulting/populating ``cache`` so the
    same reference is never fetched twice within one resolution pass."""
    if spec in cache:
        return cache[spec]
    name, property_, profile = _parse_ref(spec)
    raw = _fetch_parameter(name, profile)
    value = _extract_property(raw, property_, name) if property_ else raw
    cache[spec] = value
    return value


def resolve_string(value: str, *, cache: dict[str, str]) -> str:
    """Replace every ``{{ssm:...}}`` occurrence in ``value`` with its
    resolved value. Strings with no reference are returned unchanged
    (and never trigger an AWS call)."""
    if "{{ssm:" not in value:
        return value
    return _REF_RE.sub(lambda m: resolve_ssm_ref(m.group(1), cache=cache), value)


def resolve_value(value: Any, *, cache: dict[str, str]) -> Any:
    """Recursively resolve ``{{ssm:...}}`` references nested in dicts/lists."""
    if isinstance(value, str):
        return resolve_string(value, cache=cache)
    if isinstance(value, dict):
        return {k: resolve_value(v, cache=cache) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, cache=cache) for v in value]
    return value


def resolve_connection(conn: Connection) -> Connection:
    """Return a copy of ``conn`` with every ``{{ssm:...}}`` placeholder in
    its string fields (including nested tunnel blocks) resolved.

    Call this right before a connection is actually used (opening a tunnel,
    building an engine) — never at ``connections.yaml`` load time or from a
    listing/display command, so browsing the registry never makes an AWS
    call or risks echoing a decrypted secret to the screen.
    """
    from dbctl.config import Connection

    raw = conn.model_dump()
    resolved = resolve_value(raw, cache={})
    return Connection.model_validate(resolved)


__all__ = [
    "RefResolutionError",
    "resolve_connection",
    "resolve_ssm_ref",
    "resolve_string",
    "resolve_value",
]
