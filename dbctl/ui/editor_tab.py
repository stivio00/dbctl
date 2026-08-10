"""SQL editor tab: a syntax-highlighted TextArea bound to one connection,
with a results table below it. Ctrl+R (or the Run button) executes the
typed SQL directly against the connection's SQLAlchemy engine."""

from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from textual.app import ComposeResult
from textual.widgets import DataTable, Static, TextArea

from dbctl.audit import append as audit_append
from dbctl.db import fmt_db_error
from dbctl.ui import results
from dbctl.ui.screens import ConfirmScreen
from dbctl.ui.session import SessionManager
from dbctl.ui.tabs import RunnableTab

# A statement is treated as read-only (no confirmation / read_only gate)
# when it starts with one of these keywords, ignoring leading whitespace
# and `--` line comments. Ad-hoc SQL has no declared `mode`, unlike a
# YAML-defined operation, so this heuristic stands in for it.
_READ_ONLY_START = re.compile(r"^\s*(--[^\n]*\n\s*)*(select|with|explain|show|pragma)\b", re.IGNORECASE)


def is_write_statement(sql: str) -> bool:
    return not _READ_ONLY_START.match(sql or "")


class SqlEditorPane(RunnableTab):
    """One SQL editor tab: ``TextArea(language="sql")`` + results DataTable."""

    def __init__(
        self,
        conn_name: str,
        sessions: SessionManager,
        initial_sql: str,
        profile: str | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conn_name = conn_name
        self._sessions = sessions
        self._initial_sql = initial_sql
        self._profile = profile

    def compose_editor(self) -> ComposeResult:
        yield Static(f"[b]{self.conn_name}[/b] — Ctrl+R to run", classes="pane-header")
        yield TextArea.code_editor(self._initial_sql, language="sql", id="sql-input")

    def run_tab(self) -> None:
        table = self.query_one("#results-table", DataTable)
        session = self._sessions.get(self.conn_name)
        if not session.connected:
            results.show_message(table, f"{self.conn_name} is not connected (press 'c' in the tree)")
            return
        sql = self.query_one("#sql-input", TextArea).text.strip()
        if not sql:
            return
        write = is_write_statement(sql)
        if write and session.conn.safety.read_only:
            results.show_message(
                table, f"connection {self.conn_name!r} is read-only; refusing to run a write statement"
            )
            return
        if write and session.conn.safety.confirm:
            self.app.push_screen(
                ConfirmScreen(f"Run this statement against {self.conn_name}?"),
                lambda ok: self._execute(sql) if ok else None,
            )
            return
        self._execute(sql)

    def _execute(self, sql: str) -> None:
        session = self._sessions.get(self.conn_name)
        table = self.query_one("#results-table", DataTable)
        assert session.engine is not None  # guarded by run_tab's `connected` check
        started = time.monotonic()
        try:
            with session.engine.begin() as conn:
                result = conn.execute(text(sql))
                rows: list[dict[str, Any]] | None = None
                if result.returns_rows:
                    rows = [dict(r) for r in result.mappings()]
                    rows_affected = len(rows)
                else:
                    rows_affected = result.rowcount
        except SQLAlchemyError as e:
            audit_append(
                profile=self._profile,
                connection=self.conn_name,
                operation=None,
                params={"sql": sql},
                mode="ui-sql",
                status="error",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            results.show_message(table, fmt_db_error(e))
            return
        duration_ms = (time.monotonic() - started) * 1000
        audit_append(
            profile=self._profile,
            connection=self.conn_name,
            operation=None,
            params={"sql": sql},
            mode="ui-sql",
            status="ok",
            rows_affected=rows_affected,
            duration_ms=duration_ms,
        )
        if rows is not None:
            results.show_rows(table, rows)
        else:
            results.show_message(table, f"OK, {rows_affected} row(s) affected ({duration_ms:.1f}ms)")
