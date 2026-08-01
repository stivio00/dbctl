"""Output formatting + side-by-side diff rendering."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from dbctl.config import OutputFormat

_console = Console()


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
                delta = (None if va is None or vb is None else float(va) - float(vb))
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
        _console.print(
            f"[dim]rows only in {label_a}: {only_a} | only in {label_b}: {only_b}[/dim]"
        )


def _fmt_delta(d: Any) -> str:
    if isinstance(d, str) or d is None:
        return "[dim]n/a[/dim]"
    if d == 0:
        return "[green]0[/green]"
    if d > 0:
        return f"[red]+{d}[/red]"
    return f"[green]{d}[/green]"