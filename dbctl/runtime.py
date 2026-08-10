"""Shared runtime helpers used by Click commands.

Centralises: console handles, profile-aware registries loading, and a
context-manager that opens tunnel+engine+healthcheck in one shot so command
callbacks stay small.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
from rich.console import Console

from dbctl.connections import ConnectionsFileError
from dbctl.connections import load as load_connections
from dbctl.operations import OperationsFileError
from dbctl.operations import load as load_operations

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from dbctl.config import Connection

console = Console()
err_console = Console(stderr=True)


def registries(ctx: click.Context) -> tuple[dict, dict]:
    """Returns (conns, ops) for the active profile.

    Returns empty registries on missing config files; a malformed file is
    reported once on stderr with a continuation hint, and treated as empty so
    that `--help` and the dashboard still render instead of crashing mid-parse.
    """
    prof = ctx.obj.get("profile") if ctx.obj else None
    try:
        conns = load_connections(profile=prof)
    except ConnectionsFileError as e:
        # Surface per-connection errors concisely, but still serve the
        # connections that /did/ validate so a single mis-configured
        # reference template doesn't take down the whole CLI.
        err_console.print(f"[red]connections.yaml:[/red] {e}")
        conns = e.valid
    except Exception as e:  # noqa: BLE001 - YAML parse error, IO, etc.
        err_console.print(f"[red]connections.yaml:[/red] {e}")
        conns = {}
    try:
        ops = load_operations(profile=prof)
    except OperationsFileError as e:
        # Surface per-op errors concisely, but still serve the operations
        # that /did/ validate so a single mis-declared op doesn't take the
        # whole CLI down.
        err_console.print(f"[red]operations.yaml:[/red] {e}")
        ops = e.valid
    except Exception as e:  # noqa: BLE001 - YAML parse error, IO, etc.
        err_console.print(f"[red]operations.yaml:[/red] {e}")
        ops = {}
    return conns, ops


def confirm_or_abort(prompt: str, *, yes: bool) -> None:
    if yes:
        return
    if not click.confirm(prompt, default=False):
        raise SystemExit(0)


@dataclass
class OpenedStub:
    name: str
    conn: Connection
    engine: Engine
    tunnel: object


@contextmanager
def opened_conn(ctx: click.Context, name: str) -> Iterator[tuple[str, Connection, OpenedStub]]:
    """Open tunnel + build engine + run healthcheck.

    Yields (canonical_name, conn, stub). Tears the tunnel down on exit.
    Exits the process with a descriptive message on tunnel / DB / health
    errors so command callbacks don't need to handle those states.
    """
    from dbctl.connections import resolve
    from dbctl.db import DBError, build_engine
    from dbctl.tunnels.base import build_tunnel

    conns, _ = registries(ctx)
    try:
        canonical, conn = resolve(name, conns)
    except KeyError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(2)

    tun = build_tunnel(conn)
    try:
        tun.__enter__()
    except RuntimeError as e:
        err_console.print(f"[red]tunnel error:[/red] {e}")
        sys.exit(3)
    try:
        engine = build_engine(conn, tun)
    except DBError as e:
        tun.__exit__(None, None, None)
        err_console.print(f"[red]db error:[/red] {e}")
        sys.exit(4)

    _healthcheck(ctx, canonical, conn, engine, tun)
    stub = OpenedStub(canonical, conn, engine, tun)
    try:
        yield canonical, conn, stub
    finally:
        tun.__exit__(None, None, None)


@contextmanager
def opened_engine(ctx: click.Context, canonical: str, conn: Connection) -> Iterator[OpenedStub]:
    """Like ``opened_conn`` but takes a pre-built ``Connection`` (instead of
    resolving a name from the registry). Used by ``dbctl execute`` for
    inline SQLAlchemy URLs — the connection's ``direct`` block (a
    placeholder host:port) is unused since ``url:`` overrides it; the
    tunnel is a no-op ``DirectTunnel`` in that case.

    Yields ``OpenedStub``. Tears the tunnel down on exit. Exits with the
    same exit-code convention as ``opened_conn`` (3 tunnel, 4 db, 5 health).
    """
    from dbctl.db import DBError, build_engine
    from dbctl.tunnels.base import build_tunnel

    tun = build_tunnel(conn)
    try:
        tun.__enter__()
    except RuntimeError as e:
        err_console.print(f"[red]tunnel error:[/red] {e}")
        sys.exit(3)
    try:
        engine = build_engine(conn, tun)
    except DBError as e:
        tun.__exit__(None, None, None)
        err_console.print(f"[red]db error:[/red] {e}")
        sys.exit(4)

    _healthcheck(ctx, canonical, conn, engine, tun)
    stub = OpenedStub(canonical, conn, engine, tun)
    try:
        yield stub
    finally:
        tun.__exit__(None, None, None)


def _healthcheck(
    ctx: click.Context,
    canonical: str,
    conn: Connection,
    engine: Engine,
    tun: object,
) -> None:
    """Shared healthcheck prefix for `opened_conn` / `opened_engine`."""
    from dbctl.db import healthcheck

    if ctx.obj.get("skip_healthcheck"):
        return
    ok, ms, msg = healthcheck(engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
    if not ok:
        tun.__exit__(None, None, None)  # type: ignore[attr-defined]
        err_console.print(f"[red]healthcheck failed:[/red] {msg}")
        sys.exit(5)
    if ctx.obj.get("verbose"):
        console.print(f"[dim]health {canonical}: ok ({ms:.1f}ms)[/dim]")
