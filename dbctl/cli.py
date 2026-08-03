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
import re
import sys
import time
from typing import Any

import click
import yaml
from rich.table import Table
from sqlalchemy.exc import SQLAlchemyError

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
# Negative-number hint for dynamic commands
# --------------------------------------------------------------------------- #
_NEG_OPT_RE = re.compile(r"No such option '(-\d[^']*)'")


class _NegNumberHintCommand(click.Command):
    """Click can't parse a negative number as a positional Argument value
    (only Options support negative-number detection, and only with a Range
    type and `min < 0`). When the user writes ``dbctl pg increase-credits
    zelda -10`` Click reports ``No such option '-1'`` which is opaque.

    This subclass intercepts that exact error and appends a hint pointing
    the user at the ``--`` separator: ``dbctl <conn> <op> ... -- <args>``.
    See https://github.com/pallets/click/issues/2169 for the upstream
    limitation.
    """

    def parse_args(self, ctx, args):
        # Click's parser mutates `args` in place while it consumes tokens,
        # so by the time the except block runs the list is empty. Snapshot
        # here while still pristine.
        original_args = list(args)
        try:
            return super().parse_args(ctx, args)
        except click.exceptions.UsageError as e:
            m = _NEG_OPT_RE.match(e.message or "")
            if m:
                # Click truncates the bad token to the first option char
                # (e.g. `-10` → `-1`), so scan the original argv for the
                # offending token to show the user what *they* typed.
                bad = next(
                    (a for a in original_args if a.startswith("-") and len(a) > 1 and a[1].isdigit()),
                    m.group(1),
                )
                e.message = (
                    f"{e.message}\n  hint: a positional argument can't start "
                    f"with '-'. Separate options from positionals with '--':\n"
                    f"  {ctx.command_path} [opts] -- <positional args including {bad}>"
                )
            raise


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
    return _NegNumberHintCommand(
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
        except (RuntimeError, SQLAlchemyError) as e:
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
            err_console.print(f"[red]{_fmt_db_error(e)}[/red]")
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


# Lazy import keeps the module import-time 轻; the dynamic CLI callback
# pulls this only when an error actually surfaces.
def _fmt_db_error(e: BaseException) -> str:
    from dbctl.db import fmt_db_error

    return fmt_db_error(e)


# --------------------------------------------------------------------------- #
# multi-connection command builder (see `_make_multi_group` below)
# --------------------------------------------------------------------------- #


def _make_multi_group(verb: str, ops):
    """Build a *deprecated verb-first* top-level group for one multi verb,
    with one subcommand per declared operation. Each subcommand takes the
    role-connections as leading positional args (matching ``roles`` in
    declared order) followed by the operation's own declared parameters.

        dbctl diff user-count pg my                     # deprecated alias
        dbctl user-count pg my                          # operation-first (preferred)

    Click deprecation: clicking any verb-first command prints a warning
    pointing at the operation-first form. The verb-first group is kept so
    muscle memory and existing scripts keep working — the actual work is
    routed to the same operation-first command builder.
    """
    matching = {n: o for n, o in ops.items() if o.scope.value == "multi" and o.mode.value == verb}
    if not matching:
        return None

    @click.group(
        name=verb,
        help=(
            f"Multi-connection `{verb}` operations (deprecated verb-first form; "
            f"prefer `dbctl <op> ...`). Ops: {', '.join(matching)}"
        ),
        deprecated=True,
    )
    def group():
        pass

    for op_name, op in matching.items():
        group.add_command(_make_multi_op_command(verb, op_name, op, deprecated=True))
    return group


def _copy_click_params(op: Operation) -> list[click.Parameter]:
    """Emit per-mode CLI flags for multi ops: copy gets ``--batch-size`` /
    ``--on-conflict`` / ``--dry-run``; sync gets ``--dry-run`` /
    ``--delete-extras``; replay gets ``--batch-size`` / ``--dry-run``.
    The callback pops these by name regardless of whether the op emits them,
    so missing flags default to ``None`` / ``False``.
    """
    flags: list[click.Parameter] = []
    if op.copy_spec is not None:
        from dbctl.config import OnConflict

        flags.append(
            click.Option(
                ["--batch-size"],
                type=click.INT,
                default=op.copy_spec.batch_size,
                help=f"Rows per executemany batch (default {op.copy_spec.batch_size}).",
            )
        )
        flags.append(
            click.Option(
                ["--on-conflict"],
                type=click.Choice([m.value for m in OnConflict]),
                default=op.copy_spec.on_conflict.value,
                help="Conflict handling when target rows already exist.",
            )
        )
        flags.append(
            click.Option(
                ["--dry-run/--no-dry-run"],
                default=False,
                help="Read src + simulate inserts; write nothing to trg.",
            )
        )
    elif op.sync_spec is not None:
        flags.append(
            click.Option(
                ["--dry-run/--no-dry-run"],
                default=False,
                help="Diff src vs trg and report counts without writing.",
            )
        )
        flags.append(
            click.Option(
                ["--delete-extras/--no-delete-extras"],
                default=op.sync_spec.delete_extras,
                help="Delete trg rows whose key is absent in src (default off).",
            )
        )
    elif op.replay_spec is not None:
        flags.append(
            click.Option(
                ["--batch-size"],
                type=click.INT,
                default=op.replay_spec.batch_size,
                help=f"Rows per executemany batch (default {op.replay_spec.batch_size}).",
            )
        )
        flags.append(
            click.Option(
                ["--dry-run/--no-dry-run"],
                default=False,
                help="Read src + simulate inserts; write nothing to trg.",
            )
        )
    return flags


def _make_multi_op_command(
    verb: str, op_name: str, op: Operation, *, deprecated: bool = False
) -> click.Command:
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
    # `--show-sql` is useful for diff/compare; harmless for copy (the
    # introspected SELECT * won't be reported).
    click_params.append(click.Option(["--show-sql"], is_flag=True, help="Print resolved SQL per role."))
    click_params.extend(_copy_click_params(op))

    def callback(**kwargs: Any):
        ctx = click.get_current_context()
        show_sql = bool(kwargs.pop("show_sql", False))
        # Click lowercases parameter names when populating kwargs (so an
        # Argument declared as `["SRC"]` arrives as the key `src`). The
        # roles in the operation declaration are already lowercase, so pop
        # by their declared name — `r.upper()` would KeyError every run.
        role_conns = {r: kwargs.pop(r, None) for r in op.roles}

        # copy-mode flags
        batch_size = kwargs.pop("batch_size", None)
        on_conflict = kwargs.pop("on_conflict", None)
        dry_run = bool(kwargs.pop("dry_run", False))
        delete_extras = kwargs.pop("delete_extras", None)

        from dbctl.audit import append
        from dbctl.connections import resolve
        from dbctl.execute import bind_params
        from dbctl.multi import opened
        from dbctl.reports import render_rows, render_side_by_side

        conns_all, _ = registries(ctx)
        bound = bind_params(op, kwargs)

        # We default src/trg to roles[0]/roles[1] so copy/diff both work
        # whether the user wrote `dbctl copy-users mssql pg` (operation-first)
        # or `dbctl diff user-count pg my` (deprecated verb-first).
        role_objs: list[tuple[str, str, Any]] = []
        opened_list: list = []
        try:
            for r in op.roles:
                cname = role_conns[r]
                try:
                    canonical, conn = resolve(cname, conns_all)
                except KeyError as e:
                    err_console.print(f"[red]{e}[/red]")
                    raise SystemExit(2)
                oc = opened(canonical, conn).__enter__()
                opened_list.append(oc)
                role_objs.append((r, canonical, oc))
        except SystemExit:
            raise
        except (RuntimeError, SQLAlchemyError) as e:
            err_console.print(f"[red]{_fmt_db_error(e)}[/red]")
            raise SystemExit(1)
        # finally-free cleanup runs unconditionally after dispatch below

        started = time.monotonic()
        mode = op.mode.value
        try:
            if mode == "copy":
                _do_copy(ctx, op, op_name, role_objs, on_conflict, batch_size, dry_run, started)
                return  # _do_copy also handles audit + render + cleanup
            if mode == "sync":
                _do_sync(ctx, op, op_name, role_objs, dry_run, delete_extras, started)
                return
            if mode == "validate":
                _do_validate(ctx, op, op_name, role_objs, started)
                return
            if mode == "replay":
                _do_replay(ctx, op, op_name, role_objs, batch_size, dry_run, started)
                return
            if mode == "diff" and op.diff and op.diff.strategy.value == "table_counts":
                _do_table_counts_diff(ctx, op, op_name, role_objs, started)
                # _do_table_counts_diff also handles cleanup
                return

            # default: existing diff-with-queries / compare.
            # re-run via run_role on the opened engines; we no longer need
            # multi-opened here because role_objs already has the opened
            # engines (the previous loop opened everything).
            from dbctl.multi import run_role

            results: dict[str, list[dict]] = {}
            for r, _canonical, oc in role_objs:
                results[r] = run_role(op, r, oc, bound)

            if show_sql:
                for role, sql in (op.queries or {}).items():
                    console.print(f"[cyan]{role} SQL:[/cyan] {sql.strip()}")
        except SystemExit:
            raise
        except (RuntimeError, SQLAlchemyError) as e:
            err_console.print(f"[red]{_fmt_db_error(e)}[/red]")
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
            mode=mode,
            status="ok",
            duration_ms=(time.monotonic() - started) * 1000,
            actor=ctx.obj.get("actor"),
        )

        if mode == "diff":
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
    return _NegNumberHintCommand(
        name=op_name,
        params=click_params,
        callback=callback,
        help=f"{(op.description or '').strip()}\n\n  dbctl {op_name} {sig}".rstrip(),
        short_help=(op.description or op_name).strip(),
        deprecated=deprecated,
    )


# --------------------------------------------------------------------------- #
# copy-mode dispatch
# --------------------------------------------------------------------------- #
def _do_copy(
    ctx,
    op,
    op_name,
    role_objs,
    on_conflict,
    batch_size,
    dry_run,
    started,
) -> None:
    """Drive `run_copy` against the opened src/trg engines and render."""
    from dbctl.audit import append
    from dbctl.config import OnConflict
    from dbctl.multi import CopyReport, run_copy  # noqa: F401 (CopyReport for typing)
    from dbctl.reports import render_copy_report

    if op.roles != ["src", "trg"]:
        err_console.print(f"[red]copy operation {op_name!r}: roles must be [src, trg], got {op.roles}[/red]")
        raise SystemExit(2)
    if op.copy_spec is None:
        err_console.print(f"[red]copy operation {op_name!r}: missing copy_spec[/red]")
        raise SystemExit(2)

    spec = op.copy_spec.model_copy()  # work on a copy so spec mutation is local
    if on_conflict is not None:
        try:
            spec.on_conflict = OnConflict(on_conflict)
        except ValueError:
            err_console.print(f"[red]unknown on_conflict value: {on_conflict!r}[/red]")
            raise SystemExit(2)
    if on_conflict == "truncate":
        # `--on-conflict truncate` is a CLI shortcut for the spec's
        # `truncate_first: true`; keep them aligned when mixed.
        spec.truncate_first = True
        spec.on_conflict = OnConflict.error
    if dry_run:
        spec.truncate_first = False

    src_role, _, src_oc = role_objs[0]
    trg_role, _, trg_oc = role_objs[1]
    if dry_run:
        console.print(
            f"[yellow]dry-run:[/yellow] reading from {src_role} and simulating "
            f"inserts into {trg_role}; nothing will be written"
        )
    report = run_copy(src_oc, trg_oc, spec, batch_size=batch_size, dry_run=dry_run)

    role_conns_str = "|".join(ctx.params.get(r.upper(), r) or r for r in op.roles)
    append(
        profile=ctx.obj.get("profile"),
        connection=role_conns_str,
        operation=op_name,
        params={"batch_size": batch_size or spec.batch_size, "on_conflict": spec.on_conflict.value},
        mode="copy",
        status="dry-run" if dry_run else "ok",
        duration_ms=(time.monotonic() - started) * 1000,
        actor=ctx.obj.get("actor"),
    )
    render_copy_report(report, title=f"{op_name}: {role_conns_str}")


def _do_table_counts_diff(ctx, op, op_name, role_objs, started) -> None:
    """Auto-gen `SELECT COUNT(*) FROM <t>` per declared table."""
    from dbctl.audit import append
    from dbctl.multi import run_table_counts
    from dbctl.reports import render_side_by_side

    if op.roles != ["src", "trg"]:
        err_console.print(
            f"[red]table_counts diff {op_name!r}: roles must be [src, trg], got {op.roles}[/red]"
        )
        raise SystemExit(2)
    if op.diff is None or op.diff.tables is None:
        err_console.print(f"[red]table_counts diff {op_name!r}: `diff.tables` list required[/red]")
        raise SystemExit(2)

    src_role, src_canonical, src_oc = role_objs[0]
    trg_role, trg_canonical, trg_oc = role_objs[1]
    results = run_table_counts(src_oc, trg_oc, op.diff.tables or [])

    append(
        profile=ctx.obj.get("profile"),
        connection=f"{src_canonical}|{trg_canonical}",
        operation=op_name,
        params={},
        mode="diff",
        status="ok",
        duration_ms=(time.monotonic() - started) * 1000,
        actor=ctx.obj.get("actor"),
    )
    render_side_by_side(
        results["src"],
        results["trg"],
        key=["t"],
        show=op.diff.show if op.diff else None,
        label_a=src_canonical,
        label_b=trg_canonical,
        title=f"{op_name}: {src_canonical} vs {trg_canonical}",
    )


# --------------------------------------------------------------------------- #
# sync-mode dispatch
# --------------------------------------------------------------------------- #
def _do_sync(ctx, op, op_name, role_objs, dry_run, delete_extras, started) -> None:
    """Drive `run_sync` to converge trg table to match src."""
    from dbctl.audit import append
    from dbctl.multi import run_sync
    from dbctl.reports import render_sync_report

    if op.roles != ["src", "trg"]:
        err_console.print(f"[red]sync operation {op_name!r}: roles must be [src, trg], got {op.roles}[/red]")
        raise SystemExit(2)
    if op.sync_spec is None:
        err_console.print(f"[red]sync operation {op_name!r}: missing sync_spec[/red]")
        raise SystemExit(2)
    if not op.queries or "src" not in op.queries or "trg" not in op.queries:
        err_console.print(
            f"[red]sync operation {op_name!r}: requires queries.src (SELECT) + queries.trg (SELECT)[/red]"
        )
        raise SystemExit(2)

    spec = op.sync_spec.model_copy()
    if delete_extras is not None:
        spec.delete_extras = bool(delete_extras)

    src_role, _, src_oc = role_objs[0]
    trg_role, _, trg_oc = role_objs[1]
    if dry_run:
        console.print(
            f"[yellow]dry-run:[/yellow] diffing {src_role} → {trg_role} on "
            f"{spec.target_table!r}; nothing will be written"
        )
    report = run_sync(src_oc, trg_oc, spec, op.queries, dry_run=dry_run)

    role_conns_str = "|".join(ctx.params.get(r.upper(), r) or r for r in op.roles)
    append(
        profile=ctx.obj.get("profile"),
        connection=role_conns_str,
        operation=op_name,
        params={"delete_extras": spec.delete_extras, "dry_run": dry_run},
        mode="sync",
        status="dry-run" if dry_run else "ok",
        duration_ms=(time.monotonic() - started) * 1000,
        actor=ctx.obj.get("actor"),
    )
    render_sync_report(report, title=f"{op_name}: {role_conns_str}")


# --------------------------------------------------------------------------- #
# validate-mode dispatch
# --------------------------------------------------------------------------- #
def _do_validate(ctx, op, op_name, role_objs, started) -> None:
    """Drive `run_validate` to diff schemas (columns + types) src vs trg."""
    from dbctl.audit import append
    from dbctl.multi import run_validate
    from dbctl.reports import render_validate_report

    if op.roles != ["src", "trg"]:
        err_console.print(
            f"[red]validate operation {op_name!r}: roles must be [src, trg], got {op.roles}[/red]"
        )
        raise SystemExit(2)
    if op.validate_spec is None:
        err_console.print(f"[red]validate operation {op_name!r}: missing validate_spec[/red]")
        raise SystemExit(2)

    src_role, src_canonical, src_oc = role_objs[0]
    trg_role, trg_canonical, trg_oc = role_objs[1]
    report = run_validate(src_oc, trg_oc, op.validate_spec)

    append(
        profile=ctx.obj.get("profile"),
        connection=f"{src_canonical}|{trg_canonical}",
        operation=op_name,
        params={
            "tables": op.validate_spec.tables or [],
            "include": op.validate_spec.include,
            "exclude": op.validate_spec.exclude,
        },
        mode="validate",
        status="ok" if not report.mismatches else "drift",
        duration_ms=(time.monotonic() - started) * 1000,
        actor=ctx.obj.get("actor"),
    )
    render_validate_report(report, title=f"{op_name}: {src_canonical} vs {trg_canonical}")


# --------------------------------------------------------------------------- #
# replay-mode dispatch
# --------------------------------------------------------------------------- #
def _do_replay(ctx, op, op_name, role_objs, batch_size, dry_run, started) -> None:
    """Drive `run_replay` (copy + per-row transform)."""
    from dbctl.audit import append
    from dbctl.multi import run_replay  # noqa: F401 (CopyReport for typing)
    from dbctl.reports import render_copy_report

    if op.roles != ["src", "trg"]:
        err_console.print(
            f"[red]replay operation {op_name!r}: roles must be [src, trg], got {op.roles}[/red]"
        )
        raise SystemExit(2)
    if op.replay_spec is None:
        err_console.print(f"[red]replay operation {op_name!r}: missing replay_spec[/red]")
        raise SystemExit(2)

    src_role, _, src_oc = role_objs[0]
    trg_role, _, trg_oc = role_objs[1]
    if dry_run:
        console.print(
            f"[yellow]dry-run:[/yellow] replaying {src_role} → {trg_role} with transform "
            f"{op.replay_spec.transform!r}; nothing will be written"
        )
    report = run_replay(src_oc, trg_oc, op.replay_spec, batch_size=batch_size, dry_run=dry_run)

    role_conns_str = "|".join(ctx.params.get(r.upper(), r) or r for r in op.roles)
    append(
        profile=ctx.obj.get("profile"),
        connection=role_conns_str,
        operation=op_name,
        params={
            "batch_size": batch_size or op.replay_spec.batch_size,
            "transform": op.replay_spec.transform,
        },
        mode="replay",
        status="dry-run" if dry_run else "ok",
        duration_ms=(time.monotonic() - started) * 1000,
        actor=ctx.obj.get("actor"),
    )
    render_copy_report(report, title=f"{op_name}: {role_conns_str}")


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
    # multi-op operation-first top-level commands + deprecated verb-first groups
    multi_ops = {n for n, o in ops.items() if o.scope.value == "multi"}
    multi_modes = {o.mode.value for o in ops.values() if o.scope.value == "multi"}
    verb_first_aliases = [
        m for m in ("diff", "compare", "sync", "copy", "validate", "replay") if m in multi_modes
    ]
    return sorted(set(static) | set(multi_ops) | set(verb_first_aliases) | set(conns) | set(_aliases(conns)))


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
    # Operation-first: a multi-op registered at the top level. Takes priority
    # over a connection with the same name (the user controls both YAMLs;
    # collision surfaces a `dbctl init` warning).
    if name in ops and ops[name].scope.value == "multi":
        return _make_multi_op_command(ops[name].mode.value, name, ops[name])
    if name in conns or name in _aliases(conns):
        return LazyConnGroup(name)
    multi_modes = {o.mode.value for o in ops.values() if o.scope.value == "multi"}
    # Deprecated verb-first alias: `dbctl diff user-count pg my` still works
    if name in {"diff", "compare", "sync", "copy", "validate", "replay"} and name in multi_modes:
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
    from dbctl.connections import resolve

    conns, _ = registries(ctx)
    try:
        canonical, conn = resolve(name, conns)
    except KeyError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(2)
    console.print(
        yaml.safe_dump({canonical: conn.model_dump(mode="json")}, sort_keys=False, allow_unicode=True)
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
    from dbctl.operations import OperationsFileError
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
    except OperationsFileError as e:
        # Per-op friendly lines + a tally; raises the exit code.
        err_console.print(f"[red]operations.yaml:[/red] {e}")
        errs += len(e.errors)
        ops = e.valid
    except Exception as e:  # noqa: BLE001 - YAML parse error, IO, etc.
        err_console.print(f"[red]{type(e).__name__}: {e}[/red]")
        errs += 1
        ops = {}
    else:
        console.print(f"[green]all {len(ops)} operations valid[/green]")
    # surface any obviously-undeclared params / sql gaps even when some
    # operations failed to load (only the valid subset is checked here).
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
            # multi-DB modes that don't need explicit `queries:`
            # because they introspect: copy (uses copy_spec),
            # table_counts diff (builds SELECT COUNT(*) per table).
            strategy = op.diff.strategy.value if op.diff else "custom"
            needs_queries = not (
                op.mode.value == "copy"
                or strategy == "table_counts"
                or op.mode.value in {"validate", "replay"}  # no queries needed
            )
            if needs_queries:
                err_console.print(f"[red]{name}:[/red] multi-scope but no queries")
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
