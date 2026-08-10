"""Operation launcher tab: a parameter form for one declared single-scope
operation, bound to one connection, with a results table below it.

The actual DB round trip runs in a background thread (`@work(thread=True)`)
so the UI stays responsive and the loading indicator can actually animate -
Textual can't paint anything, including a spinner, while the main thread is
blocked on a synchronous call.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Label, Static, Switch

from dbctl.audit import append as audit_append
from dbctl.config import Operation, ParamType
from dbctl.db import fmt_db_error
from dbctl.execute import ExecResult, bind_params
from dbctl.execute import render as render_query
from dbctl.ui import results
from dbctl.ui.screens import ConfirmScreen
from dbctl.ui.session import SessionManager
from dbctl.ui.tabs import RunnableTab


class OperationPane(RunnableTab):
    """One operation-launcher tab: parameter form + results DataTable."""

    def __init__(
        self,
        conn_name: str,
        op_name: str,
        op: Operation,
        sessions: SessionManager,
        profile: str | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conn_name = conn_name
        self.op_name = op_name
        self.op = op
        self._sessions = sessions
        self._profile = profile

    def compose_editor(self) -> ComposeResult:
        yield Static(f"[b]{self.conn_name}[/b] :: {self.op_name} — Ctrl+R to run", classes="pane-header")
        with Vertical(id="param-form"):
            for p in self.op.parameters:
                with Horizontal(classes="param-row"):
                    yield Label(p.description or p.name)
                    if p.type is ParamType.bool:
                        yield Switch(value=bool(p.default), id=f"param-{p.name}")
                    else:
                        yield Input(
                            value="" if p.default is None else str(p.default),
                            placeholder=p.name,
                            password=p.type is ParamType.secret,
                            id=f"param-{p.name}",
                        )

    def _collect_params(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for p in self.op.parameters:
            widget = self.query_one(f"#param-{p.name}")
            if isinstance(widget, Switch):
                raw[p.name] = widget.value
            else:
                assert isinstance(widget, Input)
                raw[p.name] = widget.value or None
        return raw

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
        conn = session.conn
        is_dml = self.op.mode.value in {"execute", "upsert", "script"}
        if is_dml and conn.safety.read_only:
            self._show_message(f"connection {self.conn_name!r} is read-only; cannot run {self.op_name!r}")
            return
        if conn.safety.allowed_operations and self.op_name not in conn.safety.allowed_operations:
            self._show_message(f"operation {self.op_name!r} not allowed on {self.conn_name!r}")
            return
        try:
            bound = bind_params(self.op, self._collect_params())
        except ValueError as e:
            self._show_message(str(e))
            return

        needs_confirm = is_dml and (self.op.confirm or conn.safety.confirm)
        if needs_confirm:
            self.app.push_screen(
                ConfirmScreen(f"Apply {self.op_name!r} to {self.conn_name!r}?"),
                lambda ok: self._start_execute(bound) if ok else None,
            )
            return
        self._start_execute(bound)

    def _show_message(self, message: str) -> None:
        results.show_message(self.query_one("#results-table", DataTable), message)
        self.set_status(message)

    def _start_execute(self, bound: dict[str, Any]) -> None:
        self.show_loading()
        self.set_status("running…")
        self._execute_worker(bound)

    @work(thread=True)
    def _execute_worker(self, bound: dict[str, Any]) -> None:
        session = self._sessions.get_or_none(self.conn_name)
        if session is None or session.engine is None:
            self.app.call_from_thread(self._apply_result, None, "connection is no longer available")
            return
        try:
            with session.engine.begin() as sa_conn:
                res = render_query(sa_conn, self.op, bound)
        except (RuntimeError, SQLAlchemyError) as e:
            audit_append(
                profile=self._profile,
                connection=self.conn_name,
                operation=self.op_name,
                params=bound,
                mode=self.op.mode.value,
                status="error",
                redact=self._secret_names(),
            )
            self.app.call_from_thread(self._apply_result, None, fmt_db_error(e))
            return

        audit_append(
            profile=self._profile,
            connection=self.conn_name,
            operation=self.op_name,
            params=bound,
            mode=self.op.mode.value,
            status="ok",
            rows_affected=res.rows_affected,
            duration_ms=res.latency_ms,
            redact=self._secret_names(),
        )
        self.app.call_from_thread(self._apply_result, res, None)

    def _apply_result(self, res: ExecResult | None, error: str | None) -> None:
        if not self.is_mounted:
            return  # the tab was closed while the operation was running
        self.hide_loading()
        table = self.query_one("#results-table", DataTable)
        if error is not None or res is None:
            results.show_message(table, error or "unknown error")
            self.set_status("error")
            return
        if res.rows is not None:
            results.show_rows(table, res.rows)
            self.set_status(f"{len(res.rows)} row(s) · {res.latency_ms:.1f} ms")
        else:
            results.show_message(table, f"OK, {res.rows_affected} row(s) affected ({res.latency_ms:.1f}ms)")
            self.set_status(f"{res.rows_affected} row(s) affected · {res.latency_ms:.1f} ms")

    def _secret_names(self) -> set[str]:
        return {p.name for p in self.op.parameters if p.type is ParamType.secret}
