"""Left pane: a lazily-loaded schema browser (connections -> ... -> columns),
plus connect/disconnect/test/edit actions bound to the highlighted node's
nearest connection ancestor.

Two view modes, toggled with `m`:
  - simple: connection -> table -> column        (flat, quick table pick)
  - normal: connection -> schema -> Tables/Views -> table/view -> Columns/Indexes

A separate toggle, `g`, switches between a flat connection list and a
name-based grouping that treats `-` as a path separator (`in-gateway-ifp`
nests under `in-gateway`) - see `dbctl.ui.grouping`. A connection name can
be both a group and a real connection at once (`ifp` and `ifp-gateway` both
exist); such a node gets a synthetic "(this connection)" child so its own
schema stays reachable alongside its sub-connections.

Expanding a connection node connects it first if needed (like clicking a
datasource in a DB GUI) - `c`/`d`/`t` remain available for explicit control
without drilling into the tree.

Connect/disconnect/test-tunnel all run in a background thread
(`@work(thread=True)`) - tunnel setup can take several seconds (spawning
`aws`/`kubectl`/`ssh`/`az`/`gcloud`), and Textual can't paint anything,
including a spinner, while the main thread is blocked on a synchronous
call. The affected node's own label is animated as a spinner while its
worker runs, since `Tree` doesn't support embedding a widget per node.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual import work
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from dbctl.config import Connection, connections_path
from dbctl.connections import load as load_connections
from dbctl.ui import schema as introspect
from dbctl.ui.grouping import GroupNode, build_connection_groups
from dbctl.ui.session import SessionManager

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from textual.timer import Timer

# node kinds that represent (or lazily load) an actual connection's schema.
_CONNECTION_KINDS = ("connection", "connection-self")

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass(frozen=True)
class NodeData:
    # kind: connection|connection-self|group|schema|tables-group|views-group
    #       |table|view|columns-group|indexes-group|column|index|loading|error
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
        Binding("g", "toggle_group_mode", "Group/flat"),
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
        self.group_mode = False
        # background connect/disconnect/test-tunnel bookkeeping, keyed by
        # connection name - see `_start_spinner` / `_connect_in_background`.
        self._pending: set[str] = set()
        self._spinners: dict[str, Timer] = {}
        self._on_connected: dict[str, Callable[[], None]] = {}

    def on_mount(self) -> None:
        self._populate()

    # ------------------------------------------------------------------ #
    # top-level connection nodes
    # ------------------------------------------------------------------ #
    def _populate(self) -> None:
        self.root.remove_children()
        self._conn_nodes.clear()
        if self.group_mode:
            for group in build_connection_groups(sorted(self._connections)):
                self._add_group_node(self.root, group)
        else:
            for name in sorted(self._connections):
                self._add_connection_leaf(self.root, name)

    def _add_connection_leaf(self, parent: TreeNode[NodeData], name: str) -> None:
        conn = self._connections[name]
        session = self._sessions.get(name)
        node = parent.add(
            _connection_label(name, conn, session.connected, session.error),
            data=NodeData(kind="connection", conn_name=name),
        )
        self._add_placeholder(node, name)
        self._conn_nodes[name] = node

    def _add_group_node(self, parent: TreeNode[NodeData], group: GroupNode) -> None:
        if group.conn_name is None:
            # pure name-based folder - not a real connection, not connectable.
            folder = parent.add(f"[dim]{group.label}/[/dim]", data=NodeData(kind="group", conn_name=""))
            for child in group.children:
                self._add_group_node(folder, child)
            return

        if not group.children:
            # leaf connection, possibly nested - identical to flat mode.
            self._add_connection_leaf(parent, group.conn_name)
            return

        # dual-purpose: a real connection that's ALSO a folder for further
        # sub-connections - a synthetic "(this connection)" child keeps its
        # own schema reachable alongside its siblings.
        conn = self._connections[group.conn_name]
        session = self._sessions.get(group.conn_name)
        node = parent.add(
            _connection_label(group.conn_name, conn, session.connected, session.error),
            data=NodeData(kind="connection", conn_name=group.conn_name),
        )
        self._conn_nodes[group.conn_name] = node
        self_node = node.add(
            "[dim](this connection)[/dim]", data=NodeData(kind="connection-self", conn_name=group.conn_name)
        )
        self._add_placeholder(self_node, group.conn_name)
        for child in group.children:
            self._add_group_node(node, child)

    def _refresh_label(self, name: str) -> None:
        conn = self._connections[name]
        session = self._sessions.get(name)
        self._conn_nodes[name].set_label(_connection_label(name, conn, session.connected, session.error))

    def _schema_node_for(self, name: str) -> TreeNode[NodeData] | None:
        """The node whose children are this connection's lazily-loaded
        schema: the connection node itself, or its synthetic "this
        connection" child when the node is also a name-based group."""
        node = self._conn_nodes.get(name)
        if node is None:
            return None
        for child in node.children:
            if child.data is not None and child.data.kind == "connection-self":
                return child
        return node

    def connection_name_at_cursor(self) -> str | None:
        node = self.cursor_node
        while node is not None:
            if node.data is not None and node.data.kind in _CONNECTION_KINDS:
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
        if data.kind in _CONNECTION_KINDS:
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
            "connection-self": self._load_connection,
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
        self._connect_in_background(data.conn_name, lambda: self._finish_load_connection(node, data))

    def _finish_load_connection(self, node: TreeNode[NodeData], data: NodeData) -> None:
        session = self._sessions.get_or_none(data.conn_name)
        node.remove_children()
        if session is None or not session.connected or session.engine is None:
            error = session.error if session is not None else None
            node.add_leaf(
                f"[red]{error or 'not connected'}[/red]",
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

    def action_toggle_group_mode(self) -> None:
        self.group_mode = not self.group_mode
        self._populate()
        self.app.notify(f"connection tree: {'grouped' if self.group_mode else 'flat'} view")

    # ------------------------------------------------------------------ #
    # background connect + spinner - tunnel setup can spawn a subprocess
    # (aws/kubectl/ssh/az/gcloud) and take several seconds, so it always
    # runs off the main thread; the node's own label animates meanwhile.
    # ------------------------------------------------------------------ #
    def _start_spinner(self, name: str) -> None:
        node = self._conn_nodes.get(name)
        if node is None or name in self._spinners:
            return
        frames = _SPINNER_FRAMES
        suffix = f"[dim]({self._connections[name].type.value})[/dim]"
        state = {"i": 0}

        def tick() -> None:
            node.set_label(f"{frames[state['i'] % len(frames)]} {name} {suffix}")
            state["i"] += 1

        self._spinners[name] = self.set_interval(0.1, tick)

    def _stop_spinner(self, name: str) -> None:
        timer = self._spinners.pop(name, None)
        if timer is not None:
            timer.stop()

    def _connect_in_background(self, name: str, on_connected: Callable[[], None]) -> None:
        """Runs `SessionManager.connect(name)` in a worker thread (already
        idempotent/instant if it's connected), then calls `on_connected`
        back on the main thread once done."""
        session = self._sessions.get_or_none(name)
        if session is not None and session.connected:
            on_connected()
            return
        if name in self._pending:
            return
        self._pending.add(name)
        self._on_connected[name] = on_connected
        self._start_spinner(name)
        self._connect_worker(name)

    @work(thread=True)
    def _connect_worker(self, name: str) -> None:
        self._sessions.connect(name)
        self.app.call_from_thread(self._connect_worker_done, name)

    def _connect_worker_done(self, name: str) -> None:
        self._pending.discard(name)
        self._stop_spinner(name)
        self._refresh_label(name)
        callback = self._on_connected.pop(name, None)
        if callback is not None:
            callback()

    # ------------------------------------------------------------------ #
    # connect / disconnect / test / add / edit
    # ------------------------------------------------------------------ #
    def action_connect_selected(self) -> None:
        name = self.connection_name_at_cursor()
        if not name:
            return
        self._connect_in_background(name, lambda: self._notify_connect_result(name))

    def _notify_connect_result(self, name: str) -> None:
        session = self._sessions.get_or_none(name)
        if session is None:
            return
        if session.error:
            self.app.notify(f"{name}: {session.error}", severity="error", timeout=8)
        else:
            self.app.notify(f"{name}: connected")

    def action_disconnect_selected(self) -> None:
        name = self.connection_name_at_cursor()
        if not name or name in self._pending:
            return
        self._pending.add(name)
        self._start_spinner(name)
        self._disconnect_worker(name)

    @work(thread=True)
    def _disconnect_worker(self, name: str) -> None:
        self._sessions.disconnect(name)
        self.app.call_from_thread(self._disconnect_worker_done, name)

    def _disconnect_worker_done(self, name: str) -> None:
        self._pending.discard(name)
        self._stop_spinner(name)
        self._refresh_label(name)
        # Reset only the schema-bearing node - a group-mode hybrid node's
        # sub-connections are independent and must not be wiped out too.
        node = self._schema_node_for(name)
        if node is not None:
            node.remove_children()
            self._add_placeholder(node, name)
        self.app.notify(f"{name}: disconnected")

    def action_test_tunnel_selected(self) -> None:
        name = self.connection_name_at_cursor()
        if not name or name in self._pending:
            return
        self._pending.add(name)
        self._start_spinner(name)
        self._test_tunnel_worker(name)

    @work(thread=True)
    def _test_tunnel_worker(self, name: str) -> None:
        ok, msg = self._sessions.test_tunnel(name)
        self.app.call_from_thread(self._test_tunnel_worker_done, name, ok, msg)

    def _test_tunnel_worker_done(self, name: str, ok: bool, msg: str) -> None:
        self._pending.discard(name)
        self._stop_spinner(name)
        self._refresh_label(name)
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
