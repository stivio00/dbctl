"""Operation launcher tab: a parameter form for one declared single-scope
operation, bound to one connection, with a results table below it."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Label, Static, Switch

from dbctl.audit import append as audit_append
from dbctl.config import Operation, ParamType
from dbctl.db import fmt_db_error
from dbctl.execute import bind_params
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
        table = self.query_one("#results-table", DataTable)
        session = self._sessions.get_or_none(self.conn_name)
        if session is None:
            results.show_message(table, f"connection {self.conn_name!r} no longer exists (removed?)")
            return
        if not session.connected:
            results.show_message(table, f"{self.conn_name} is not connected (press 'c' in the tree)")
            return
        conn = session.conn
        is_dml = self.op.mode.value in {"execute", "upsert", "script"}
        if is_dml and conn.safety.read_only:
            results.show_message(
                table, f"connection {self.conn_name!r} is read-only; cannot run {self.op_name!r}"
            )
            return
        if conn.safety.allowed_operations and self.op_name not in conn.safety.allowed_operations:
            results.show_message(table, f"operation {self.op_name!r} not allowed on {self.conn_name!r}")
            return
        try:
            bound = bind_params(self.op, self._collect_params())
        except ValueError as e:
            results.show_message(table, str(e))
            return

        needs_confirm = is_dml and (self.op.confirm or conn.safety.confirm)
        if needs_confirm:
            self.app.push_screen(
                ConfirmScreen(f"Apply {self.op_name!r} to {self.conn_name!r}?"),
                lambda ok: self._execute(bound) if ok else None,
            )
            return
        self._execute(bound)

    def _execute(self, bound: dict[str, Any]) -> None:
        session = self._sessions.get(self.conn_name)
        table = self.query_one("#results-table", DataTable)
        assert session.engine is not None  # guarded by run_tab's `connected` check
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
            results.show_message(table, fmt_db_error(e))
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
        if res.rows is not None:
            results.show_rows(table, res.rows)
        else:
            results.show_message(table, f"OK, {res.rows_affected} row(s) affected ({res.latency_ms:.1f}ms)")

    def _secret_names(self) -> set[str]:
        return {p.name for p in self.op.parameters if p.type is ParamType.secret}
