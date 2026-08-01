"""dbctl CLI.

Top-level group hosts static commands (status, doctor, init, connections,
operations, history, tunnel) plus a *dynamic* LazyGroup that synthesizes a
subcommand per configured connection. Each connection group holds one
subcommand per *single-scope* operation declared in operations.yaml.
Multi-scope operations surface as top-level groups ``diff`` / ``compare`` /
``sync`` (whichever modes are present in the registry).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any

import click
import yaml
from rich.table import Table

from dbctl import __version__
from dbctl.config import Operation, OutputFormat, Param, ParamType
from dbctl.operations import by_scope
from dbctl.operations import resolve as resolve_op
from dbctl.runtime import (
    confirm_or_abort,
    console,
    err_console,
    opened_conn,
    registries,
)


# --------------------------------------------------------------------------- #
# param type mapping
# --------------------------------------------------------------------------- #
def _click_type(p: Param):
    match p.type:
        case ParamType.integer:
            return click.INT
        case ParamType.float:
            return click.FLOAT
        case ParamType.bool:
            return click.BOOL
        case ParamType.path:
            return click.Path()
        case _:
            return click.STRING


# --------------------------------------------------------------------------- #
# connection page (LazyConnGroup)
# --------------------------------------------------------------------------- #
class LazyConnGroup(click.Group):
    """Synthesizes subcommands for one configured connection."""

    def __init__(self, conn_name: str, **kwargs):
        super().__init__(name=conn_name, **kwargs)
        self.conn_name = conn_name

    def list_commands(self, ctx: click.Context) -> list[str]:
        _, ops = registries(ctx)
        singles = by_scope(ops, "single")
        return sorted(set(singles) | {"health", "info", "history", "again"})

    def get_command(self, ctx: click.Context, name: str):
        if name in {"health", "info", "history", "again"}:
            return _static_conn_cmd(name)
        _, ops = registries(ctx)
        singles = by_scope(ops, "single")
        if name in singles:
            return _make_single_op_command(self.conn_name, name, singles[name])
        return None


# --------------------------------------------------------------------------- #
# static per-connection commands (health / info / history / again)
# --------------------------------------------------------------------------- #
def _static_conn_cmd(name: str) -> click.Command:
    if name == "health":

        @click.command(name="health", help="Run the configured healthcheck query.")
        def health() -> None:
            ctx = click.get_current_context()
            conn_name = ctx.parent.info_name
            with opened_conn(ctx, conn_name) as (canonical, conn, stub):
                from dbctl.db import healthcheck as hc

                ok, ms, msg = hc(stub.engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
                color = "green" if ok else "red"
                console.print(f"[{color}]{'OK' if ok else 'FAIL'}[/{color}] {canonical} ({ms:.1f}ms)")
                if not ok:
                    console.print(f"  [dim]{msg}[/dim]")
                raise SystemExit(0 if ok else 5)

        return health

    if name == "info":

        @click.command(name="info", help="Run a named info query (or all).")
        @click.argument("qname", required=False)
        def info(qname: str) -> None:
            ctx = click.get_current_context()
            conn_name = ctx.parent.info_name
            with opened_conn(ctx, conn_name) as (canonical, conn, stub):
                from sqlalchemy import text

                from dbctl.reports import render_rows

                if not conn.info:
                    console.print("[dim]no info queries defined[/dim]")
                    return
                selected = [q for q in conn.info if not qname or q.name == qname]
                if qname and not selected:
                    err_console.print(f"[red]no info query named {qname!r}[/red]")
                    raise SystemExit(2)
                with stub.engine.connect() as c:
                    for q in selected:
                        rows = [dict(r) for r in c.execute(text(q.query)).mappings()]
                        render_rows(rows, "table", title=f"info: {q.name} ({canonical})")

        return info

    if name == "history":

        @click.command(name="history", help="Show recent operations run against this connection.")
        def history() -> None:
            ctx = click.get_current_context()
            conn_name = ctx.parent.info_name
            from dbctl.connections import resolve

            conns, _ = registries(ctx)
            canonical, _ = resolve(conn_name, conns)
            from dbctl.audit import read

            rows = [e for e in read(ctx.obj.get("profile")) if e.get("connection") == canonical]
            render_history_table(rows[-30:])

        return history

    if name == "again":

        @click.command(name="again", help="Re-run the last operation on this connection.")
        def again() -> None:
            ctx = click.get_current_context()
            conn_name = ctx.parent.info_name
            from dbctl.connections import resolve

            conns, ops = registries(ctx)
            canonical, conn = resolve(conn_name, conns)
            from dbctl.audit import last_for

            last = last_for(ctx.obj.get("profile"), canonical)
            if not last:
                err_console.print(f"[red]no previous operation found for {canonical}[/red]")
                raise SystemExit(2)
            op_name = last["operation"]
            params = last.get("params") or {}
            console.print(f"[dim]re-running:[/dim] {canonical} {op_name} {params}")
            op = resolve_op(op_name, ops)
            _execute_single(ctx, canonical, conn, op_name, op, params, apply=True, yes=True)

        return again

    return None


# --------------------------------------------------------------------------- #
# single-op command builder
# --------------------------------------------------------------------------- #
def _make_single_op_command(conn_name: str, op_name: str, op: Operation) -> click.Command:
    pos_params = [p for p in op.parameters if p.position is not None]
    pos_params.sort(key=lambda p: p.position)
    keyword_params = [p for p in op.parameters if p.position is None]

    click_params: list[click.Parameter] = []
    for p in pos_params:
        click_params.append(
            click.Argument(
                [p.name],
                required=p.required and p.default is None,
                default=p.default,
                type=_click_type(p),
            )
        )
    for p in keyword_params:
        click_params.append(
            click.Option(
                [f"--{p.name}"],
                required=p.required,
                default=p.default,
                type=_click_type(p),
                help=p.description or None,
                show_default=p.default is not None,
            )
        )
    click_params.append(
        click.Option(["--apply"], is_flag=True, help="Commit (default dry-runs when confirm).")
    )
    click_params.append(click.Option(["--yes", "-y"], is_flag=True, help="Skip confirmation prompt."))
    click_params.append(
        click.Option(["--show-sql"], is_flag=True, help="Print resolved SQL before executing.")
    )
    click_params.append(
        click.Option(
            ["--output", "-o"],
            type=click.Choice([m.value for m in OutputFormat]),
            default=None,
            help="Override output format (for fetch modes).",
        )
    )

    def callback(**kwargs: Any):
        ctx = click.get_current_context()
        apply_flag = bool(kwargs.pop("apply", False))
        yes = bool(kwargs.pop("yes", False))
        show_sql = bool(kwargs.pop("show_sql", False))
        out_fmt = kwargs.pop("output", None)
        from dbctl.connections import resolve

        conns, _ = registries(ctx)
        canonical, conn = resolve(conn_name, conns)
        kwargs = _prompt_missing(op, kwargs)
        fmt = out_fmt or op.output.value
        _execute_single(
            ctx,
            canonical,
            conn,
            op_name,
            op,
            kwargs,
            apply=apply_flag,
            yes=yes,
            show_sql=show_sql,
            fmt=fmt,
        )

    sig = " ".join(p.name.upper() for p in pos_params)
    help_text = (op.description or "").strip()
    if sig:
        help_text = f"{help_text}\n\n  dbctl {conn_name} {op_name} {sig}"
    return click.Command(
        name=op_name,
        params=click_params,
        callback=callback,
        help=help_text,
        short_help=(op.description or op_name).strip(),
    )


def _prompt_missing(op: Operation, kwargs: dict) -> dict:
    if not sys.stdin.isatty():
        return kwargs
    for p in op.parameters:
        val = kwargs.get(p.name)
        if (val is None or val == "") and p.required and p.default is None:
            is_secret = p.type is ParamType.secret
            val = click.prompt(
                f"{p.name} ({p.description or 'value'})",
                type=_click_type(p),
                hide_input=is_secret,
                confirmation_prompt=is_secret,
            )
            kwargs[p.name] = val
    return kwargs


# --------------------------------------------------------------------------- #
# single-op execution
# --------------------------------------------------------------------------- #
def _execute_single(
    ctx: click.Context,
    canonical: str,
    conn,
    op_name: str,
    op: Operation,
    params: dict,
    *,
    apply: bool,
    yes: bool,
    show_sql: bool = False,
    fmt: str = "table",
) -> None:
    from dbctl.audit import append
    from dbctl.execute import bind_params, format_sql
    from dbctl.execute import render as render_query
    from dbctl.reports import render_rows

    is_dml = op.mode.value in {"execute", "upsert", "script"}
    read_only = conn.safety.read_only and is_dml
    needs_confirm = is_dml and (op.confirm or conn.safety.confirm) and not read_only

    if read_only:
        err_console.print(f"[red]connection {canonical!r} is read-only; cannot run {op_name!r}[/red]")
        raise SystemExit(6)
    if conn.safety.allowed_operations and op_name not in conn.safety.allowed_operations:
        err_console.print(f"[red]operation {op_name!r} not allowed on {canonical!r}[/red]")
        raise SystemExit(6)

    try:
        bound = bind_params(op, params)
    except ValueError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(2)

    if show_sql:
        console.print(f"[cyan]resolved SQL:[/cyan]\n{format_sql(op, bound)}")

    if needs_confirm and not apply:
        console.print("[yellow]dry-run (use --apply to commit)[/yellow]")
        append(
            profile=ctx.obj.get("profile"),
            connection=canonical,
            operation=op_name,
            params=bound,
            mode=op.mode.value,
            status="dry-run",
            actor=ctx.obj.get("actor"),
            redact=_secret_names(op),
        )
        raise SystemExit(0)

    with opened_conn(ctx, canonical) as (name, _conn, stub):
        # Confirm BEFORE opening the transaction so "no" leaves the DB untouched.
        if needs_confirm and not yes:
            confirm_or_abort(f"Apply {op_name} to {canonical}?", yes=yes)
        started = time.monotonic()
        try:
            with stub.engine.begin() as sa_conn:
                res = render_query(sa_conn, op, bound)
            rows = res.rows
            rows_affected = res.rows_affected
            status = "ok"
        except RuntimeError as e:
            append(
                profile=ctx.obj.get("profile"),
                connection=canonical,
                operation=op_name,
                params=bound,
                mode=op.mode.value,
                status="error",
                duration_ms=(time.monotonic() - started) * 1000,
                actor=ctx.obj.get("actor"),
                redact=_secret_names(op),
            )
            err_console.print(f"[red]{e}[/red]")
            raise SystemExit(1)

        if rows is not None:
            render_rows(rows, fmt, title=f"{canonical} :: {op_name}")
        else:
            console.print(f"[green]OK[/green] {rows_affected} row(s) affected in {res.latency_ms:.1f}ms")

        append(
            profile=ctx.obj.get("profile"),
            connection=canonical,
            operation=op_name,
            params=bound,
            mode=op.mode.value,
            status=status,
            rows_affected=rows_affected,
            duration_ms=res.latency_ms,
            actor=ctx.obj.get("actor"),
            redact=_secret_names(op),
        )


def _secret_names(op: Operation) -> set[str]:
    return {p.name for p in op.parameters if p.type is ParamType.secret}


# --------------------------------------------------------------------------- #
# multi-connection command builder (see `_make_multi_group` below)
# --------------------------------------------------------------------------- #


def _make_multi_group(verb: str, ops):
    """Build a top-level group for one multi verb, with one subcommand per
    declared operation. Each subcommand takes the role-connections as leading
    positional args (matching ``roles`` in declared order) followed by the
    operation's own declared parameters.

        dbctl diff user-count pg my
        dbctl diff compare-quotas pg my Daily
        dbctl compare top-users pg my --limit 5
    """
    matching = {n: o for n, o in ops.items() if o.scope.value == "multi" and o.mode.value == verb}
    if not matching:
        return None

    @click.group(name=verb, help=f"Multi-connection `{verb}` operations: {', '.join(matching)}")
    def group():
        pass

    for op_name, op in matching.items():
        group.add_command(_make_multi_op_command(verb, op_name, op))
    return group


def _make_multi_op_command(verb: str, op_name: str, op: Operation) -> click.Command:
    pos_params = [p for p in op.parameters if p.position is not None]
    pos_params.sort(key=lambda p: p.position)
    keyword_params = [p for p in op.parameters if p.position is None]

    # First positional args are the role connections (matching roles order);
    # then the operation's own positional params.
    click_params: list[click.Parameter] = []
    for r in op.roles:
        click_params.append(click.Argument([r.upper()], required=True, metavar=r.upper()))
    for p in pos_params:
        click_params.append(
            click.Argument([p.name], required=p.required, default=p.default, type=_click_type(p))
        )
    for p in keyword_params:
        click_params.append(
            click.Option(
                [f"--{p.name}"],
                required=p.required,
                default=p.default,
                type=_click_type(p),
                help=p.description or None,
            )
        )
    click_params.append(click.Option(["--show-sql"], is_flag=True, help="Print resolved SQL per role."))

    def callback(**kwargs: Any):
        ctx = click.get_current_context()
        show_sql = bool(kwargs.pop("show_sql", False))
        # Click lowercases parameter names when populating kwargs (so an
        # Argument declared as `["SRC"]` arrives as the key `src`). The
        # roles in the operation declaration are already lowercase, so pop
        # by their declared name — `r.upper()` would KeyError every run.
        role_conns = {r: kwargs.pop(r, None) for r in op.roles}

        from dbctl.audit import append
        from dbctl.connections import resolve
        from dbctl.execute import bind_params
        from dbctl.multi import opened, run_role
        from dbctl.reports import render_rows, render_side_by_side

        conns_all, _ = registries(ctx)
        bound = bind_params(op, kwargs)

        if show_sql:
            for role, sql in (op.queries or {}).items():
                console.print(f"[cyan]{role} SQL:[/cyan] {sql.strip()}")

        opened_list = []
        results: dict[str, list[dict]] = {}
        started = time.monotonic()
        try:
            for role in op.roles:
                cname = role_conns[role]
                canonical, conn = resolve(cname, conns_all)
                oc = opened(canonical, conn).__enter__()
                opened_list.append(oc)
                results[role] = run_role(op, role, oc, bound)
        except RuntimeError as e:
            err_console.print(f"[red]{e}[/red]")
            raise SystemExit(1)
        finally:
            for oc in opened_list:
                with contextlib.suppress(Exception):
                    oc.tunnel.__exit__(None, None, None)

        append(
            profile=ctx.obj.get("profile"),
            connection="|".join(role_conns[r] for r in op.roles),
            operation=op_name,
            params=bound,
            mode=op.mode.value,
            status="ok",
            duration_ms=(time.monotonic() - started) * 1000,
            actor=ctx.obj.get("actor"),
        )

        if op.mode.value == "diff":
            diff = op.diff
            sample = results.get(op.roles[0]) or results.get(op.roles[1]) or []
            key = diff.key if diff else (list(sample[0].keys())[:1] if sample else [])
            show = diff.show if diff else None
            render_side_by_side(
                results[op.roles[0]],
                results[op.roles[1]],
                key=key,
                show=show,
                label_a=role_conns[op.roles[0]],
                label_b=role_conns[op.roles[1]],
                title=f"{op_name}: {role_conns[op.roles[0]]} vs {role_conns[op.roles[1]]}",
            )
        else:
            for role, rows in results.items():
                render_rows(rows, op.output.value, title=f"{role_conns[role]} :: {op_name}")

    sig = " ".join(r.upper() for r in op.roles) + (
        " " + " ".join(p.name.upper() for p in pos_params) if pos_params else ""
    )
    return click.Command(
        name=op_name,
        params=click_params,
        callback=callback,
        help=f"{(op.description or '').strip()}\n\n  dbctl {verb} {op_name} {sig}".rstrip(),
        short_help=(op.description or op_name).strip(),
    )


# --------------------------------------------------------------------------- #
# top-level root group with dynamic connection lookup
# --------------------------------------------------------------------------- #
def _aliases(conns):
    out = []
    for c in conns.values():
        out.extend(c.aliases)
    return out


def _root_list(ctx: click.Context) -> list[str]:
    conns, ops = registries(ctx)
    static = ["connections", "operations", "status", "doctor", "init", "history", "tunnel"]
    multi_modes = {o.mode.value for o in ops.values() if o.scope.value == "multi"}
    multi_commands = [m for m in ("diff", "compare", "sync") if m in multi_modes]
    return sorted(set(static) | set(multi_commands) | set(conns) | set(_aliases(conns)))


def _root_get(ctx: click.Context, name: str):
    static = {
        "connections": connections_cmd,
        "operations": operations_cmd,
        "status": status_cmd,
        "doctor": doctor_cmd,
        "init": init_cmd,
        "history": history_cmd,
        "tunnel": tunnel_cmd,
    }
    if name in static:
        return static[name]
    conns, ops = registries(ctx)
    if name in conns or name in _aliases(conns):
        return LazyConnGroup(name)
    multi_modes = {o.mode.value for o in ops.values() if o.scope.value == "multi"}
    if name in {"diff", "compare", "sync"} and name in multi_modes:
        return _make_multi_group(name, ops)
    return None


# --------------------------------------------------------------------------- #
# main group
# --------------------------------------------------------------------------- #
@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dbctl")
@click.option("--profile", default=None, help="Use a profile config dir (~/.dbctl/profiles/<name>).")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output.")
@click.option("--skip-healthcheck", is_flag=True, help="Skip the connection healthcheck.")
@click.option(
    "--install-completion",
    type=click.Choice(["bash", "zsh", "fish"]),
    default=None,
    help="Print a shell completion snippet and exit.",
)
@click.pass_context
def main(ctx, profile, verbose, skip_healthcheck, install_completion):
    """dbctl - monitor, control, and administer your databases.

    Run `dbctl` bare for a status dashboard; `dbctl <conn>` for a connection
    page; `dbctl <conn> <op> ...` to run an operation.
    """
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile
    ctx.obj["verbose"] = verbose
    ctx.obj["skip_healthcheck"] = skip_healthcheck
    ctx.obj["actor"] = os.environ.get("USER") or os.environ.get("USERNAME")

    if install_completion:
        _print_completion(install_completion)
        ctx.exit(0)

    if ctx.invoked_subcommand is None:
        _dashboard(ctx)


# inject dynamic root behavior
main.list_commands = _root_list  # type: ignore[method-assign]
main.get_command = _root_get  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# static commands
# --------------------------------------------------------------------------- #
@main.group("connections")
def connections_cmd():
    """Inspect configured connections."""


@connections_cmd.command("list")
@click.pass_context
def connections_list(ctx):
    conns, _ = registries(ctx)
    table = Table(title="connections", header_style="bold cyan")
    table.add_column("name")
    table.add_column("type")
    table.add_column("driver")
    table.add_column("database")
    table.add_column("description")
    for n in sorted(conns):
        c = conns[n]
        table.add_row(n, c.type.value, c.driver, c.database, c.description)
    console.print(table)


@connections_cmd.command("show")
@click.argument("name")
@click.pass_context
def connections_show(ctx, name):
    conns, _ = registries(ctx)
    if name not in conns:
        err_console.print(f"[red]unknown connection {name!r}[/red]")
        raise SystemExit(2)
    console.print(
        yaml.safe_dump({name: conns[name].model_dump(mode="json")}, sort_keys=False, allow_unicode=True)
    )


@main.group("operations")
def operations_cmd():
    """Inspect declared operations."""


@operations_cmd.command("list")
@click.pass_context
def operations_list(ctx):
    _, ops = registries(ctx)
    table = Table(title="operations", header_style="bold cyan")
    table.add_column("name")
    table.add_column("scope")
    table.add_column("mode")
    table.add_column("description")
    for n in sorted(ops):
        o = ops[n]
        table.add_row(n, o.scope.value, o.mode.value, (o.description or "")[:60])
    console.print(table)


@operations_cmd.command("show")
@click.argument("name")
@click.pass_context
def operations_show(ctx, name):
    _, ops = registries(ctx)
    if name not in ops:
        err_console.print(f"[red]unknown operation {name!r}[/red]")
        raise SystemExit(2)
    console.print(yaml.safe_dump({name: ops[name].model_dump(mode="json")}, sort_keys=False))


@operations_cmd.command("validate")
@click.option("--strict / --no-strict", "strict", default=False, help="Fail on missing config files.")
@click.pass_context
def operations_validate(ctx, strict):
    """Validate operations.yaml by re-loading the registry and reporting
    any pydantic / yaml errors instead of crashing on `--help`."""
    from dbctl.config import operations_path
    from dbctl.operations import load as load_ops

    path = operations_path(ctx.obj.get("profile"))
    if not path.exists():
        if strict:
            err_console.print(f"[red]no operations file at {path}[/red]")
            raise SystemExit(1)
        console.print(f"[yellow]no operations file at {path}[/yellow]")
        return
    errs = 0
    try:
        ops = load_ops(path=path)
        console.print(f"[green]all {len(ops)} operations valid[/green]")
        # surface any obviously-undeclared params / sql gaps
        for name, op in ops.items():
            is_fetched_single = op.scope.value == "single" and op.mode.value in {
                "execute",
                "fetch",
                "fetch_one",
            }
            if is_fetched_single and not op.sql:
                err_console.print(f"[red]{name}:[/red] mode {op.mode.value!r} but no sql")
                errs += 1
            if op.scope.value == "multi" and not op.queries:
                err_console.print(f"[red]{name}:[/red] multi-scope but no queries")
                errs += 1
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]{type(e).__name__}: {e}[/red]")
        errs += 1
    raise SystemExit(0 if errs == 0 else 1)


@click.command("status")
@click.pass_context
def status_cmd(ctx):
    """Show the dashboard."""
    _dashboard(ctx)


@click.command("doctor")
@click.pass_context
def doctor_cmd(ctx):
    """Healthcheck all connections and report optional CLI dependencies."""
    conns, _ = registries(ctx)
    table = Table(title="doctor", header_style="bold cyan")
    table.add_column("connection")
    table.add_column("status")
    table.add_column("latency")
    table.add_column("note")
    from dbctl.db import DBError, build_engine
    from dbctl.db import healthcheck as hc
    from dbctl.tunnels.base import build_tunnel

    for n, c in conns.items():
        try:
            tun = build_tunnel(c)
            tun.__enter__()
            try:
                engine = build_engine(c, tun)
                ok, ms, msg = hc(engine, c.healthcheck.query, c.healthcheck.timeout_seconds)
                color = "green" if ok else "red"
                table.add_row(
                    n,
                    f"[{color}]{'OK' if ok else 'FAIL'}[/{color}]",
                    f"{ms:.1f}ms" if ok else "-",
                    msg if not ok else "",
                )
            finally:
                tun.__exit__(None, None, None)
        except (RuntimeError, DBError) as e:
            table.add_row(n, "[red]ERR[/red]", "-", str(e)[:80])
    console.print(table)

    _doctor_deps(ctx, conns)


def _doctor_deps(ctx, conns) -> None:
    """Print a small table of *optional* CLI tools dbctl may shell out to.

    A tool is reported as ``OK`` if found on PATH; ``missing`` otherwise.
    A tool is only reported as ``required`` when at least one configured
    connection uses the corresponding tunnel type - so a fresh repo with
    only `direct` connections won't surface noise about absent `kubectl`.
    """
    import shutil

    from dbctl.config import TunnelType

    used_types = {c.type for c in conns.values()}
    # tool -> (tunnel type that needs it, help text when missing)
    deps = [
        ("kubectl", TunnelType.k8s, "install: https://kubernetes.io/docs/tasks/tools/"),
        ("aws", TunnelType.ssm, "install: `pip install awscli` or your OS package"),
        ("ssh", TunnelType.ssh, "install: OpenSSH client (openssh-clients / openssh-client)"),
    ]

    dep_table = Table(title="optional dependencies", header_style="bold cyan")
    dep_table.add_column("tool")
    dep_table.add_column("status")
    dep_table.add_column("required by config")
    dep_table.add_column("note")

    for tool, tun_type, hint in deps:
        on_path = shutil.which(tool) is not None
        required = tun_type in used_types
        if on_path:
            status_cell = "[green]OK[/green]"
            note_cell = ""
        elif required:
            status_cell = "[red]missing[/red]"
            note_cell = f"[red]required by a '{tun_type.value}' connection[/red] — {hint}"
        else:
            status_cell = "[yellow]not on PATH[/yellow]"
            note_cell = f"only needed for '{tun_type.value}' tunnels — {hint}"
        dep_table.add_row(
            tool,
            status_cell,
            "yes" if required else "no",
            note_cell,
        )
    console.print(dep_table)


@click.command("init")
@click.pass_context
def init_cmd(ctx):
    """Interactive wizard to add a connection to ~/.dbctl/connections.yaml."""
    from dbctl.init import run_wizard

    run_wizard(profile=ctx.obj.get("profile"))


@main.group("history")
def history_cmd():
    """Show the audit log."""


@history_cmd.command("list")
@click.option("--limit", default=30, help="Last N runs.")
@click.pass_context
def history_list(ctx, limit):
    from dbctl.audit import read

    render_history_table(read(ctx.obj.get("profile"), limit=limit))


@history_cmd.command("show")
@click.argument("run_id")
@click.pass_context
def history_show(ctx, run_id):
    import json

    from dbctl.audit import read

    for e in read(ctx.obj.get("profile"), limit=1000):
        if e.get("run_id") == run_id:
            console.print_json(json.dumps(e, default=str))
            return
    err_console.print(f"[red]no run with id {run_id}[/red]")
    raise SystemExit(2)


@main.group("tunnel")
def tunnel_cmd():
    """Hold a tunnel open for ad-hoc work."""


@tunnel_cmd.command("open")
@click.argument("name")
@click.pass_context
def tunnel_open(ctx, name):
    from dbctl.connections import resolve

    conns, _ = registries(ctx)
    try:
        canonical, c = resolve(name, conns)
    except KeyError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(2)
    from dbctl.tunnels.base import build_tunnel

    tun = build_tunnel(c)
    tun.__enter__()
    console.print(f"[green]tunnel open:[/green] {tun.local_host}:{tun.local_port} -> {canonical}")
    console.print("[dim]Ctrl-C to close...[/dim]")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        tun.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# dashboard + history rendering
# --------------------------------------------------------------------------- #
def _dashboard(ctx: click.Context) -> None:
    conns, ops = registries(ctx)
    if not conns:
        console.print(
            "[yellow]No connections configured yet.[/yellow] "
            "Run [bold]dbctl init[/bold] to add one, or edit "
            "[bold]~/.dbctl/connections.yaml[/bold]."
        )
        return
    table = Table(title="dbctl dashboard", header_style="bold cyan", show_lines=True)
    table.add_column("conn")
    table.add_column("type")
    table.add_column("driver")
    table.add_column("database")
    table.add_column("ops")
    table.add_column("description")
    single_count = sum(1 for o in ops.values() if o.scope.value == "single")
    multi_count = sum(1 for o in ops.values() if o.scope.value == "multi")
    for n in sorted(conns):
        c = conns[n]
        table.add_row(
            n + (f" [dim]({','.join(c.aliases)})[/dim]" if c.aliases else ""),
            c.type.value,
            c.driver,
            c.database,
            f"{single_count} single / {multi_count} multi",
            c.description,
        )
    console.print(table)
    console.print(
        "[dim]use `dbctl <conn>` for an overview; "
        "`dbctl <conn> <op>` to run an operation; "
        "`dbctl diff <op> <src> <trg>` to compare databases.[/dim]"
    )


def render_history_table(rows: list[dict]):
    if not rows:
        console.print("[dim]no history yet[/dim]")
        return
    table = Table(title="history", header_style="bold cyan")
    for col in ["ts", "connection", "operation", "mode", "status", "rows", "ms", "run_id"]:
        table.add_column(col)
    for r in rows:
        status = r.get("status", "")
        color = "green" if status == "ok" else ("yellow" if status == "dry-run" else "red")
        table.add_row(
            str(r.get("ts", "")),
            str(r.get("connection", "")),
            str(r.get("operation", "")),
            str(r.get("mode", "")),
            f"[{color}]{status}[/{color}]",
            str(r.get("rows_affected") or ""),
            f"{r.get('duration_ms', 0):.0f}" if r.get("duration_ms") else "",
            str(r.get("run_id", "")),
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #
def _print_completion(shell: str) -> None:
    console.print(f"[green]add to ~/.{shell}rc:[/green]")
    console.print(f'  eval "$(_DBCTL_COMPLETE={shell}_source dbctl)"')
    console.print("[dim]then `source ~/.{shell}rc` or restart your shell.[/dim]")


if __name__ == "__main__":
    main()
