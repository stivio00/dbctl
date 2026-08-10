"""Render ``list[dict]`` rows (or a plain message) into a tab's results DataTable."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable


def show_rows(table: DataTable[Any], rows: list[dict[str, Any]]) -> None:
    table.clear(columns=True)
    if not rows:
        table.add_column("result")
        table.add_row("(no rows)")
        return
    columns = list(rows[0].keys())
    table.add_columns(*(str(c) for c in columns))
    for row in rows:
        table.add_row(*(_cell(row.get(c)) for c in columns))


def show_message(table: DataTable[Any], message: str) -> None:
    table.clear(columns=True)
    table.add_column("result")
    table.add_row(message)


def _cell(value: Any) -> str:
    return "" if value is None else str(value)
