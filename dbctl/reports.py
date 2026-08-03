"""Output formatting + side-by-side diff rendering."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from dbctl.config import OutputFormat

_console = Console()


def copy_progress() -> Progress:
    """Build the live per-table progress display for `copy`/`replay`.

    Driven by `multi.run_copy`'s `on_progress("start"|"progress"|"done",
    table, rows)` hook — one spinner-tracked row per table, with a running
    row count (no percentage/ETA, since the total row count isn't known
    without an extra COUNT(*) pass the copy deliberately avoids). Falls back
    to plain sequential output automatically when stdout isn't a terminal
    (e.g. piped to a file or captured by a test runner).

    Uses the ASCII ``"line"`` spinner style (``-\\|/``) rather than rich's
    default braille glyphs, which fall outside the legacy Windows console's
    cp1252 code page and raise `UnicodeEncodeError` there.
    """
    return Progress(
        SpinnerColumn(spinner_name="line"),
        TextColumn("[bold cyan]{task.fields[table]}"),
        TextColumn("{task.completed:,} rows"),
        TimeElapsedColumn(),
        console=_console,
    )


def _rows_for_table(rows: list[dict]) -> tuple[list[str], list[list[Any]]]:
    if not rows:
        return [], []
    cols = list(rows[0].keys())
    body = [[r.get(c, "") for c in cols] for r in rows]
    return cols, body


def render_rows(rows: list[dict], fmt: OutputFormat | str = "table", *, title: str = "") -> None:
    fmt = OutputFormat(fmt) if isinstance(fmt, str) else fmt
    if fmt is OutputFormat.json:
        _console.print_json(json.dumps(rows, default=str))
        return
    if fmt is OutputFormat.csv:
        cols, body = _rows_for_table(rows)
        buf = io.StringIO()
        w = csv.writer(buf)
        if cols:
            w.writerow(cols)
        w.writerows(body)
        # rich prints CSV weirdly with control chars; print raw
        print(buf.getvalue(), end="")
        return
    if fmt is OutputFormat.yaml:
        print(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True).replace("---\n", ""))
        return
    # table
    table = Table(title=title, show_lines=False, header_style="bold cyan")
    cols, body = _rows_for_table(rows)
    for c in cols:
        table.add_column(str(c))
    for r in body:
        table.add_row(*[_fmt_cell(x) for x in r])
    _console.print(table if cols else "[dim](no rows)[/dim]")


def _fmt_cell(x: Any) -> str:
    if x is None:
        return "[dim]NULL[/dim]"
    return str(x)


def render_side_by_side(
    rows_a: list[dict],
    rows_b: list[dict],
    *,
    key: list[str],
    show: list[str] | None = None,
    title: str = "",
    label_a: str = "src",
    label_b: str = "trg",
) -> None:
    """Join two result sets on `key` and print rows: key, val_a, val_b, delta.

    Used by `dbctl diff`. Assumes each row has the key columns plus one or
    more metric columns; the metric columns are picked from the first result
    set, excluding the key columns.
    """
    index_a = {tuple(r[k] for k in key): r for r in rows_a}
    index_b = {tuple(r[k] for k in key): r for r in rows_b}
    all_keys = sorted(set(index_a) | set(index_b))

    metric_cols = []
    for r in rows_a or rows_b:
        for c in r:
            if c not in key and c not in metric_cols:
                metric_cols.append(c)
        if metric_cols:
            break
    if show:
        # filter to the requested ones; keep order
        metric_cols = [c for c in metric_cols if c in show] or metric_cols

    table = Table(title=title, show_lines=False, header_style="bold cyan")
    for k in key:
        table.add_column(k)
    for c in metric_cols:
        table.add_column(f"{label_a}.{c}", justify="right")
        table.add_column(f"{label_b}.{c}", justify="right")
        table.add_column("Δ", justify="right")

    only_a = len(index_a) - len(index_a.keys() & index_b.keys())
    only_b = len(index_b) - len(index_a.keys() & index_b.keys())

    for k in all_keys:
        ra, rb = index_a.get(k, {}), index_b.get(k, {})
        row_cells: list[str] = [str(x) for x in k]
        for c in metric_cols:
            va = ra.get(c)
            vb = rb.get(c)
            try:
                delta = None if va is None or vb is None else float(va) - float(vb)
            except (TypeError, ValueError):
                delta = "n/a"
            styling_a = "[red]" if (rb and va is None) else ""
            styling_b = "[red]" if (ra and vb is None) else ""
            row_cells.extend(
                [
                    f"{styling_a}{va if va is not None else '[dim]∅[/dim]'}",
                    f"{styling_b}{vb if vb is not None else '[dim]∅[/dim]'}",
                    _fmt_delta(delta),
                ]
            )
        table.add_row(*row_cells)

    _console.print(table)
    if only_a or only_b:
        _console.print(f"[dim]rows only in {label_a}: {only_a} | only in {label_b}: {only_b}[/dim]")


def _fmt_delta(d: Any) -> str:
    if isinstance(d, str) or d is None:
        return "[dim]n/a[/dim]"
    if d == 0:
        return "[green]0[/green]"
    if d > 0:
        return f"[red]+{d}[/red]"
    return f"[green]{d}[/green]"


def render_copy_report(report, *, title: str = "") -> None:
    """Render a `CopyReport` (multi.run_copy) as a per-table summary.

    Columns: table | src_rows | trg_inserted | skipped | ms | note
    The total elapsed time + grand totals are appended as a footer line.
    """
    table = Table(title=title or "copy", header_style="bold cyan")
    for col in ("table", "src_rows", "trg_inserted", "skipped", "ms", "note"):
        table.add_column(col)
    src_total = 0
    ins_total = 0
    skip_total = 0
    for r in report.results:
        color = "green" if r.trg_rows_inserted == r.src_rows or r.note == "dry-run" else "yellow"
        table.add_row(
            r.table,
            str(r.src_rows),
            f"[{color}]{r.trg_rows_inserted}[/{color}]",
            str(r.skipped_existing),
            f"{r.duration_ms:.1f}",
            r.note,
        )
        src_total += r.src_rows
        ins_total += r.trg_rows_inserted
        skip_total += r.skipped_existing
    _console.print(table)
    _console.print(
        f"[dim]total: {ins_total} inserted / {src_total} src rows / "
        f"{skip_total} skipped in {report.total_ms:.1f}ms[/dim]"
    )


def render_sync_report(report, *, title: str = "") -> None:
    """Render a per-table summary of a `SyncReport` (multi.run_sync).

    Columns: table | src | trg | +ins | ~upd | -del | unchanged | ms | note
    """
    table = Table(title=title or "sync", header_style="bold cyan")
    for col in ("table", "src", "trg", "+ins", "~upd", "-del", "unchanged", "ms", "note"):
        table.add_column(col)
    src_total = ins_total = upd_total = del_total = unchanged_total = 0
    for r in report.results:
        table.add_row(
            r.table,
            str(r.src_rows),
            str(r.trg_rows),
            f"[green]{r.inserted}[/green]" if r.inserted else "0",
            f"[yellow]{r.updated}[/yellow]" if r.updated else "0",
            f"[red]{r.deleted}[/red]" if r.deleted else "0",
            str(r.unchanged),
            f"{r.duration_ms:.1f}",
            r.note,
        )
        src_total += r.src_rows
        ins_total += r.inserted
        upd_total += r.updated
        del_total += r.deleted
        unchanged_total += r.unchanged
    _console.print(table)
    _console.print(
        f"[dim]total: {ins_total} inserted / {upd_total} updated / "
        f"{del_total} deleted / {unchanged_total} unchanged in {report.total_ms:.1f}ms[/dim]"
    )


def render_constraint_violations(violations, *, title: str = "") -> None:
    """Render the `list[ConstraintViolation]` from `multi.check_copy_constraints`
    as a per-row mismatch table (used by `copy --validate-data`).

    Columns: table | column | kind | row | detail
    """
    table = Table(title=title or "constraint violations", header_style="bold cyan")
    for col in ("table", "column", "kind", "row", "detail"):
        table.add_column(col)
    kind_color = {"not_null": "red", "too_long": "yellow"}
    for v in violations:
        color = kind_color.get(v.kind, "white")
        table.add_row(v.table, v.column, f"[{color}]{v.kind}[/{color}]", str(v.row_index), v.detail)
    _console.print(table)
    _console.print(f"[red]{len(violations)} violation(s) found — copy aborted, nothing was written[/red]")


def render_validate_report(report, *, title: str = "") -> None:
    """Render a `ValidateReport` (multi.run_validate) as a list of mismatches.

    Columns: table | column | kind | src_type | trg_type
    When there are zero mismatches, prints a green ✓ line instead of a table.
    """
    if not report.mismatches:
        _console.print(
            f"[green]✓[/green] [dim]{title or 'validate'}: "
            f"{report.tables_compared} table(s) compared, no schema drift[/dim]"
        )
        return
    table = Table(title=title or "validate", header_style="bold cyan")
    for col in ("table", "column", "kind", "src_type", "trg_type"):
        table.add_column(col)
    for m in report.mismatches:
        kind_color = {
            "missing_in_trg": "yellow",
            "missing_in_src": "yellow",
            "type_mismatch": "red",
        }.get(m.kind, "white")
        table.add_row(m.table, m.column, f"[{kind_color}]{m.kind}[/{kind_color}]", m.src_type, m.trg_type)
    _console.print(table)
    _console.print(
        f"[dim]{len(report.mismatches)} mismatch(es) across {report.tables_compared} "
        f"table(s) in {report.duration_ms:.1f}ms[/dim]"
    )
