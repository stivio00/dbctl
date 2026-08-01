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

from dbctl.connections import load as load_connections
from dbctl.operations import load as load_operations

if TYPE_CHECKING:
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
    except Exception as e:  # noqa: BLE001 - show user-friendly error once
        err_console.print(f"[red]connections.yaml:[/red] {e}")
        conns = {}
    try:
        ops = load_operations(profile=prof)
    except Exception as e:  # noqa: BLE001
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
    engine: object
    tunnel: object


@contextmanager
def opened_conn(ctx: click.Context, name: str) -> Iterator[tuple[str, Connection, OpenedStub]]:
    """Open tunnel + build engine + run healthcheck.

    Yields (canonical_name, conn, stub). Tears the tunnel down on exit.
    Exits the process with a descriptive message on tunnel / DB / health
    errors so command callbacks don't need to handle those states.
    """
    from dbctl.connections import resolve
    from dbctl.db import DBError, build_engine, healthcheck
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

    if not ctx.obj.get("skip_healthcheck"):
        ok, _ms, msg = healthcheck(engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
        if not ok:
            tun.__exit__(None, None, None)
            err_console.print(f"[red]healthcheck failed:[/red] {msg}")
            sys.exit(5)
        if ctx.obj.get("verbose"):
            console.print(f"[dim]health {canonical}: ok ({_ms:.1f}ms)[/dim]")

    stub = OpenedStub(canonical, conn, engine, tun)
    try:
        yield canonical, conn, stub
    finally:
        tun.__exit__(None, None, None)
