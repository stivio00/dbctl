"""SQL editor tab: a syntax-highlighted TextArea bound to one connection,
with a results table below it. Ctrl+R (or the Run button) executes the
typed SQL directly against the connection's SQLAlchemy engine.

The actual DB round trip runs in a background thread (`@work(thread=True)`)
so the UI stays responsive and the loading indicator can actually animate -
Textual can't paint anything, including a spinner, while the main thread is
blocked on a synchronous call.
"""

from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from textual import work
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
        if self.running:
            return
        session = self._sessions.get_or_none(self.conn_name)
        if session is None:
            self._show_message(f"connection {self.conn_name!r} no longer exists (removed?)")
            return
        if not session.connected:
            self._show_message(f"{self.conn_name} is not connected (press 'c' in the tree)")
            return
        sql = self.query_one("#sql-input", TextArea).text.strip()
        if not sql:
            return
        write = is_write_statement(sql)
        if write and session.conn.safety.read_only:
            self._show_message(
                f"connection {self.conn_name!r} is read-only; refusing to run a write statement"
            )
            return
        if write and session.conn.safety.confirm:
            self.app.push_screen(
                ConfirmScreen(f"Run this statement against {self.conn_name}?"),
                lambda ok: self._start_execute(sql) if ok else None,
            )
            return
        self._start_execute(sql)

    def _show_message(self, message: str) -> None:
        results.show_message(self.query_one("#results-table", DataTable), message)
        self.set_status(message)

    def _start_execute(self, sql: str) -> None:
        self.show_loading()
        self.set_status("running…")
        self._execute_worker(sql)

    @work(thread=True)
    def _execute_worker(self, sql: str) -> None:
        session = self._sessions.get_or_none(self.conn_name)
        started = time.monotonic()
        if session is None or session.engine is None:
            duration_ms = (time.monotonic() - started) * 1000
            self.app.call_from_thread(
                self._apply_result, None, None, duration_ms, "connection is no longer available"
            )
            return
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
            duration_ms = (time.monotonic() - started) * 1000
            audit_append(
                profile=self._profile,
                connection=self.conn_name,
                operation=None,
                params={"sql": sql},
                mode="ui-sql",
                status="error",
                duration_ms=duration_ms,
            )
            self.app.call_from_thread(self._apply_result, None, None, duration_ms, fmt_db_error(e))
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
        self.app.call_from_thread(self._apply_result, rows, rows_affected, duration_ms, None)

    def _apply_result(
        self,
        rows: list[dict[str, Any]] | None,
        rows_affected: int | None,
        duration_ms: float,
        error: str | None,
    ) -> None:
        if not self.is_mounted:
            return  # the tab was closed while the query was running
        self.hide_loading()
        table = self.query_one("#results-table", DataTable)
        if error is not None:
            results.show_message(table, error)
            self.set_status(f"error · {duration_ms:.1f} ms")
            return
        if rows is not None:
            results.show_rows(table, rows)
            self.set_status(f"{len(rows)} row(s) · {duration_ms:.1f} ms")
        else:
            results.show_message(table, f"OK, {rows_affected} row(s) affected ({duration_ms:.1f}ms)")
            self.set_status(f"{rows_affected} row(s) affected · {duration_ms:.1f} ms")
