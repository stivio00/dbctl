"""Tolerant config loading for the UI.

Mirrors ``dbctl.runtime.registries`` (a single malformed connection or
operation is dropped with a warning instead of taking the whole registry
down) but doesn't require a ``click.Context`` since the UI isn't a Click
command.
"""

from __future__ import annotations

from dbctl.config import Connection, Operation
from dbctl.connections import ConnectionsFileError
from dbctl.connections import load as load_connections
from dbctl.operations import OperationsFileError
from dbctl.operations import load as load_operations


def load_registries(
    profile: str | None,
) -> tuple[dict[str, Connection], dict[str, Operation], list[str]]:
    """Returns ``(connections, operations, warnings)``."""
    warnings: list[str] = []
    try:
        conns = load_connections(profile=profile)
    except ConnectionsFileError as e:
        warnings.append(f"connections.yaml: {e}")
        conns = e.valid
    except Exception as e:  # noqa: BLE001 - YAML parse error, IO, etc.
        warnings.append(f"connections.yaml: {e}")
        conns = {}
    try:
        ops = load_operations(profile=profile)
    except OperationsFileError as e:
        warnings.append(f"operations.yaml: {e}")
        ops = e.valid
    except Exception as e:  # noqa: BLE001 - YAML parse error, IO, etc.
        warnings.append(f"operations.yaml: {e}")
        ops = {}
    return conns, ops, warnings
