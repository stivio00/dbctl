"""``dbctl ui`` entry point.

Composes a connection tree (left) with a tabbed workspace of SQL editor /
operation-launcher tabs (right), each with a results table below. Reuses the
same loaders/executors as the CLI - the only new state this module owns is
the per-connection tunnel+engine lifecycle in ``dbctl.ui.session.SessionManager``.
"""

from __future__ import annotations

import contextlib

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Footer, Header, TabbedContent, TabPane

from dbctl.ui.connection_tree import ConnectionActivated, ConnectionTree, TableActivated
from dbctl.ui.editor_tab import SqlEditorPane
from dbctl.ui.operation_tab import OperationPane
from dbctl.ui.registry import load_registries
from dbctl.ui.screens import NewTabScreen, OperationLauncherScreen
from dbctl.ui.session import SessionManager
from dbctl.ui.splitter import VerticalSplitter
from dbctl.ui.sql_templates import default_select, qualified_table
from dbctl.ui.tabs import EDITOR_HEIGHT_STEP, MAX_EDITOR_HEIGHT, MIN_EDITOR_HEIGHT, RunnableTab

MIN_TREE_WIDTH = 20
MAX_TREE_WIDTH = 80
DEFAULT_TREE_WIDTH = 32
TREE_WIDTH_STEP = 4


class DbctlApp(App[None]):
    """dbctl's interactive TUI: connection tree + tabbed SQL/operation workspace."""

    TITLE = "dbctl"

    CSS = """
    #connection-tree {
        border-right: solid $panel;
    }
    .pane-header {
        padding: 0 1;
        background: $panel;
    }
    .pane-toolbar {
        height: auto;
        padding: 0 1;
    }
    .param-row {
        height: auto;
        padding: 0 1;
    }
    .param-row Label {
        width: 20;
        content-align: right middle;
    }
    #sql-input, #param-form {
        height: 1fr;
    }
    #results-table {
        height: 1fr;
    }
    #confirm-dialog, #new-tab-dialog, #operation-launcher-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    """

    BINDINGS = [
        ("ctrl+r", "run_tab", "Run"),
        ("ctrl+n", "new_tab", "New tab"),
        ("ctrl+w", "close_tab", "Close tab"),
        ("ctrl+o", "launch_operation", "Run operation"),
        ("ctrl+left", "narrow_tree", "Narrow tree"),
        ("ctrl+right", "widen_tree", "Widen tree"),
        ("ctrl+up", "grow_editor", "Grow editor"),
        ("ctrl+down", "shrink_editor", "Shrink editor"),
    ]

    tree_width: reactive[int] = reactive(DEFAULT_TREE_WIDTH)

    def __init__(self, profile: str | None = None) -> None:
        super().__init__()
        self.profile = profile
        self.connections, self.operations, self._load_warnings = load_registries(profile)
        self.sessions = SessionManager(self.connections)
        self._tab_seq = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ConnectionTree(self.connections, self.sessions, profile=self.profile, id="connection-tree")
            yield VerticalSplitter(min_value=MIN_TREE_WIDTH, max_value=MAX_TREE_WIDTH, id="tree-splitter")
            yield TabbedContent(id="workspace")
        yield Footer()

    def on_mount(self) -> None:
        for warning in self._load_warnings:
            self.notify(warning, severity="warning", timeout=10)

    def watch_tree_width(self, width: int) -> None:
        with contextlib.suppress(NoMatches):  # not mounted yet - re-fires once it is
            self.query_one("#connection-tree").styles.width = width

    def action_narrow_tree(self) -> None:
        self.tree_width = max(MIN_TREE_WIDTH, self.tree_width - TREE_WIDTH_STEP)

    def action_widen_tree(self) -> None:
        self.tree_width = min(MAX_TREE_WIDTH, self.tree_width + TREE_WIDTH_STEP)

    def on_unmount(self) -> None:
        self.sessions.disconnect_all()

    @on(ConnectionActivated)
    def _open_default_tab(self, message: ConnectionActivated) -> None:
        self.open_sql_tab(message.name)

    @on(TableActivated)
    def _open_table_tab(self, message: TableActivated) -> None:
        conn = self.connections[message.conn_name]
        target = qualified_table(conn, message.table, message.schema_name)
        self.open_sql_tab(message.conn_name, sql=default_select(conn, target))

    def _next_tab_id(self) -> str:
        self._tab_seq += 1
        return f"tab-{self._tab_seq}"

    def open_sql_tab(self, conn_name: str, *, sql: str | None = None) -> None:
        tabbed = self.query_one(TabbedContent)
        tab_id = self._next_tab_id()
        initial_sql = sql or default_select(self.connections[conn_name])
        pane = SqlEditorPane(conn_name, self.sessions, initial_sql, self.profile, id=f"pane-{tab_id}")
        tabbed.add_pane(TabPane(f"{conn_name}: sql", pane, id=tab_id))
        tabbed.active = tab_id

    def open_operation_tab(self, conn_name: str, op_name: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tab_id = self._next_tab_id()
        op = self.operations[op_name]
        pane = OperationPane(conn_name, op_name, op, self.sessions, self.profile, id=f"pane-{tab_id}")
        tabbed.add_pane(TabPane(f"{conn_name}: {op_name}", pane, id=tab_id))
        tabbed.active = tab_id

    def action_new_tab(self) -> None:
        if not self.connections:
            self.notify("no connections configured", severity="warning")
            return

        def handle(result: tuple[str, str, str | None] | None) -> None:
            if result is None:
                return
            kind, conn_name, op_name = result
            if kind == "sql":
                self.open_sql_tab(conn_name)
            elif op_name is not None:
                self.open_operation_tab(conn_name, op_name)

        singles = {n: o for n, o in self.operations.items() if o.scope.value == "single"}
        self.push_screen(NewTabScreen(list(self.connections), singles), handle)

    def action_close_tab(self) -> None:
        tabbed = self.query_one(TabbedContent)
        if tabbed.active:
            tabbed.remove_pane(tabbed.active)

    def _active_runnable(self) -> RunnableTab | None:
        pane = self.query_one(TabbedContent).active_pane
        if pane is None:
            return None
        try:
            return pane.query_one(RunnableTab)
        except NoMatches:
            return None

    def action_run_tab(self) -> None:
        runnable = self._active_runnable()
        if runnable is not None:
            runnable.run_tab()

    def action_grow_editor(self) -> None:
        runnable = self._active_runnable()
        if runnable is not None:
            runnable.editor_height = min(MAX_EDITOR_HEIGHT, runnable.editor_height + EDITOR_HEIGHT_STEP)

    def action_shrink_editor(self) -> None:
        runnable = self._active_runnable()
        if runnable is not None:
            runnable.editor_height = max(MIN_EDITOR_HEIGHT, runnable.editor_height - EDITOR_HEIGHT_STEP)

    def _resolve_launch_connection(self) -> str | None:
        """Pick the connection Ctrl+O should target: the active tab's
        connection, else whatever's highlighted in the tree, else the only
        connection if there's just one."""
        runnable = self._active_runnable()
        if runnable is not None:
            return runnable.conn_name
        name = self.query_one(ConnectionTree).connection_name_at_cursor()
        if name:
            return name
        if len(self.connections) == 1:
            return next(iter(self.connections))
        return None

    def action_launch_operation(self) -> None:
        singles = {n: o for n, o in self.operations.items() if o.scope.value == "single"}
        if not singles:
            self.notify("no operations configured", severity="warning")
            return
        conn_name = self._resolve_launch_connection()
        if conn_name is None:
            self.notify("highlight a connection in the tree first", severity="warning")
            return

        def handle(op_name: str | None) -> None:
            if op_name is not None:
                self.open_operation_tab(conn_name, op_name)

        self.push_screen(OperationLauncherScreen(singles, conn_name), handle)
