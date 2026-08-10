"""Left pane: a lazily-loaded schema browser (connections -> ... -> columns),
plus connect/disconnect/test/edit actions bound to the highlighted node's
nearest connection ancestor.

Two view modes, toggled with `m`:
  - simple: connection -> table -> column        (flat, quick table pick)
  - normal: connection -> schema -> Tables/Views -> table/view -> Columns/Indexes

Expanding a connection node connects it first if needed (like clicking a
datasource in a DB GUI) - `c`/`d`/`t` remain available for explicit control
without drilling into the tree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.binding import Binding
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from dbctl.config import Connection, connections_path
from dbctl.connections import load as load_connections
from dbctl.ui import schema as introspect
from dbctl.ui.session import SessionManager

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class NodeData:
    # kind: connection|schema|tables-group|views-group|table|view
    #       |columns-group|indexes-group|column|index|loading|error
    kind: str
    conn_name: str
    schema: str | None = None
    table: str | None = None
    is_view: bool = False


def _status_icon(connected: bool, error: str | None) -> str:
    if error:
        return "[red]![/red]"
    return "[green]●[/green]" if connected else "[dim]○[/dim]"


def _connection_label(name: str, conn: Connection, connected: bool, error: str | None) -> str:
    return f"{_status_icon(connected, error)} {name} [dim]({conn.type.value})[/dim]"


class ConnectionActivated(Message):
    """Posted when the user activates (Enter) a connection node."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__()


class TableActivated(Message):
    """Posted when the user activates (Enter) a table or view node."""

    def __init__(self, conn_name: str, schema_name: str | None, table: str) -> None:
        self.conn_name = conn_name
        self.schema_name = schema_name
        self.table = table
        super().__init__()


class ConnectionTree(Tree[NodeData]):
    """Sourced from connections.yaml; c/d/t/a/e act on the highlighted node's connection."""

    BINDINGS = [
        Binding("c", "connect_selected", "Connect"),
        Binding("d", "disconnect_selected", "Disconnect"),
        Binding("t", "test_tunnel_selected", "Test tunnel"),
        Binding("a", "add_connection", "Add"),
        Binding("e", "edit_connection", "Edit"),
        Binding("m", "toggle_view_mode", "Simple/full view"),
    ]

    def __init__(
        self,
        connections: dict[str, Connection],
        sessions: SessionManager,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__("connections", **kwargs)
        self._connections = connections
        self._sessions = sessions
        self._profile = profile
        self._conn_nodes: dict[str, TreeNode[NodeData]] = {}
        self.show_root = False
        self.view_mode = "simple"  # or "normal"

    def on_mount(self) -> None:
        self._populate()

    # ------------------------------------------------------------------ #
    # top-level connection nodes
    # ------------------------------------------------------------------ #
    def _populate(self) -> None:
        self.root.remove_children()
        self._conn_nodes.clear()
        for name in sorted(self._connections):
            conn = self._connections[name]
            session = self._sessions.get(name)
            node = self.root.add(
                _connection_label(name, conn, session.connected, session.error),
                data=NodeData(kind="connection", conn_name=name),
            )
            self._add_placeholder(node, name)
            self._conn_nodes[name] = node

    def _refresh_label(self, name: str) -> None:
        conn = self._connections[name]
        session = self._sessions.get(name)
        self._conn_nodes[name].set_label(_connection_label(name, conn, session.connected, session.error))

    def _connection_name_at_cursor(self) -> str | None:
        node = self.cursor_node
        while node is not None:
            if node.data is not None and node.data.kind == "connection":
                return node.data.conn_name
            node = node.parent
        return None

    # ------------------------------------------------------------------ #
    # activation (Enter) vs expansion (Space) - Tree's `auto_expand`
    # default means Enter on a non-leaf node fires both.
    # ------------------------------------------------------------------ #
    def on_tree_node_selected(self, event: Tree.NodeSelected[NodeData]) -> None:
        data = event.node.data
        if data is None:
            return
        if data.kind == "connection":
            self.post_message(ConnectionActivated(data.conn_name))
        elif data.kind in ("table", "view") and data.table is not None:
            self.post_message(TableActivated(data.conn_name, data.schema, data.table))

    # ------------------------------------------------------------------ #
    # lazy loading: every expandable node gets one "loading" placeholder
    # leaf at creation time; NodeExpanded replaces it with real children
    # exactly once (re-expanding a node that already loaded is a no-op).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _add_placeholder(node: TreeNode[NodeData], conn_name: str) -> None:
        node.add_leaf("…", data=NodeData(kind="loading", conn_name=conn_name))

    @staticmethod
    def _is_unloaded(node: TreeNode[NodeData]) -> bool:
        children = node.children
        return len(children) == 1 and children[0].data is not None and children[0].data.kind == "loading"

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NodeData]) -> None:
        node = event.node
        data = node.data
        if data is None or not self._is_unloaded(node):
            return
        loaders = {
            "connection": self._load_connection,
            "schema": self._load_schema,
            "tables-group": self._load_tables_group,
            "views-group": self._load_views_group,
            "table": self._load_table_or_view,
            "view": self._load_table_or_view,
            "columns-group": self._load_columns_group,
            "indexes-group": self._load_indexes_group,
        }
        loader = loaders.get(data.kind)
        if loader is not None:
            loader(node, data)

    def _load_connection(self, node: TreeNode[NodeData], data: NodeData) -> None:
        session = self._sessions.connect(data.conn_name)
        self._refresh_label(data.conn_name)
        node.remove_children()
        if not session.connected or session.engine is None:
            node.add_leaf(
                f"[red]{session.error or 'not connected'}[/red]",
                data=NodeData(kind="error", conn_name=data.conn_name),
            )
            return
        if self.view_mode == "simple":
            self._add_table_nodes(node, data.conn_name, schema=None, engine=session.engine)
        else:
            schemas = introspect.list_schemas(session.engine)
            if not schemas:
                self._add_group_nodes(node, data.conn_name, schema=None)
            for sch in schemas:
                schema_node = node.add(
                    f"[b]{sch}[/b]", data=NodeData(kind="schema", conn_name=data.conn_name, schema=sch)
                )
                self._add_placeholder(schema_node, data.conn_name)

    def _load_schema(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        self._add_group_nodes(node, data.conn_name, data.schema)

    def _add_group_nodes(self, parent: TreeNode[NodeData], conn_name: str, schema: str | None) -> None:
        tables_node = parent.add(
            "Tables", data=NodeData(kind="tables-group", conn_name=conn_name, schema=schema)
        )
        self._add_placeholder(tables_node, conn_name)
        views_node = parent.add(
            "Views", data=NodeData(kind="views-group", conn_name=conn_name, schema=schema)
        )
        self._add_placeholder(views_node, conn_name)

    def _engine_for(self, conn_name: str) -> Engine | None:
        session = self._sessions.get(conn_name)
        return session.engine

    def _load_tables_group(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        engine = self._engine_for(data.conn_name)
        if engine is None:
            return
        self._add_table_nodes(node, data.conn_name, data.schema, engine, is_view=False)

    def _load_views_group(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        engine = self._engine_for(data.conn_name)
        if engine is None:
            return
        names = introspect.list_views(engine, schema=data.schema)
        for name in names:
            v_node = node.add(
                name,
                data=NodeData(
                    kind="view", conn_name=data.conn_name, schema=data.schema, table=name, is_view=True
                ),
            )
            self._add_placeholder(v_node, data.conn_name)

    def _add_table_nodes(
        self,
        parent: TreeNode[NodeData],
        conn_name: str,
        schema: str | None,
        engine: Engine,
        *,
        is_view: bool = False,
    ) -> None:
        for name in introspect.list_tables(engine, schema=schema):
            t_node = parent.add(
                name,
                data=NodeData(kind="table", conn_name=conn_name, schema=schema, table=name, is_view=is_view),
            )
            self._add_placeholder(t_node, conn_name)

    def _load_table_or_view(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        engine = self._engine_for(data.conn_name)
        if engine is None or data.table is None:
            return
        if self.view_mode == "simple":
            self._add_column_leaves(node, data, engine)
            return
        cols_node = node.add(
            "Columns",
            data=NodeData(
                kind="columns-group", conn_name=data.conn_name, schema=data.schema, table=data.table
            ),
        )
        self._add_placeholder(cols_node, data.conn_name)
        if not data.is_view:
            idx_node = node.add(
                "Indexes",
                data=NodeData(
                    kind="indexes-group", conn_name=data.conn_name, schema=data.schema, table=data.table
                ),
            )
            self._add_placeholder(idx_node, data.conn_name)

    def _load_columns_group(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        engine = self._engine_for(data.conn_name)
        if engine is None or data.table is None:
            return
        self._add_column_leaves(node, data, engine)

    def _add_column_leaves(self, parent: TreeNode[NodeData], data: NodeData, engine: Engine) -> None:
        assert data.table is not None
        for col in introspect.list_columns(engine, data.table, schema=data.schema):
            marker = " [dim](PK)[/dim]" if col.primary_key else ""
            parent.add_leaf(
                f"{col.name} [dim]{col.type}[/dim]{marker}",
                data=NodeData(kind="column", conn_name=data.conn_name, schema=data.schema, table=data.table),
            )

    def _load_indexes_group(self, node: TreeNode[NodeData], data: NodeData) -> None:
        node.remove_children()
        engine = self._engine_for(data.conn_name)
        if engine is None or data.table is None:
            return
        for ix in introspect.list_indexes(engine, data.table, schema=data.schema):
            unique = " [dim]UNIQUE[/dim]" if ix.unique else ""
            node.add_leaf(
                f"{ix.name} ({', '.join(ix.columns)}){unique}",
                data=NodeData(kind="index", conn_name=data.conn_name),
            )

    def action_toggle_view_mode(self) -> None:
        self.view_mode = "normal" if self.view_mode == "simple" else "simple"
        self._populate()
        self.app.notify(f"connection tree: {self.view_mode} view")

    # ------------------------------------------------------------------ #
    # connect / disconnect / test / add / edit
    # ------------------------------------------------------------------ #
    def action_connect_selected(self) -> None:
        name = self._connection_name_at_cursor()
        if not name:
            return
        session = self._sessions.connect(name)
        self._refresh_label(name)
        if session.error:
            self.app.notify(f"{name}: {session.error}", severity="error", timeout=8)
        else:
            self.app.notify(f"{name}: connected")

    def action_disconnect_selected(self) -> None:
        name = self._connection_name_at_cursor()
        if not name:
            return
        self._sessions.disconnect(name)
        self._refresh_label(name)
        node = self._conn_nodes.get(name)
        if node is not None:
            node.remove_children()
            self._add_placeholder(node, name)
        self.app.notify(f"{name}: disconnected")

    def action_test_tunnel_selected(self) -> None:
        name = self._connection_name_at_cursor()
        if not name:
            return
        ok, msg = self._sessions.test_tunnel(name)
        self.app.notify(f"{name}: {msg}", severity="information" if ok else "error", timeout=8)

    def action_add_connection(self) -> None:
        self._edit_file()

    def action_edit_connection(self) -> None:
        self._edit_file()

    def _edit_file(self) -> None:
        path = connections_path(self._profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("connections: {}\n", encoding="utf-8")
        editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
        with self.app.suspend():
            subprocess.run([editor, str(path)])
        try:
            connections = load_connections(profile=self._profile)
        except Exception as e:  # noqa: BLE001 - surfaced via notify, not a crash
            self.app.notify(f"connections.yaml: {e}", severity="error", timeout=10)
            return
        self._connections.clear()
        self._connections.update(connections)
        self._sessions.reload(self._connections)
        self._populate()
