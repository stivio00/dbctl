"""Group mode (`g`): the connection tree grouped by `-`-delimited name
segments, including names that are both a group and a real connection."""

from __future__ import annotations

from dbctl.ui.app import DbctlApp
from dbctl.ui.connection_tree import ConnectionTree


async def test_flat_is_the_default(grouped_registry):
    app = DbctlApp()
    async with app.run_test():
        tree = app.query_one(ConnectionTree)
        assert tree.group_mode is False
        assert set(tree._conn_nodes) == set(grouped_registry)
        # everything is a direct child of the root in flat mode.
        assert {c.data.conn_name for c in tree.root.children} == set(grouped_registry)


async def test_group_mode_creates_pure_folder_for_branching_prefix(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        prod_folder = next(c for c in tree.root.children if c.data.kind == "group")
        assert str(prod_folder.label) == "prod/"
        assert prod_folder.data.conn_name == ""

        prod_folder.expand()  # pure group node - built eagerly, no worker involved
        await pilot.pause()
        assert {c.data.conn_name for c in prod_folder.children} == {"prod-tenant1", "prod-tenant2"}
        assert all(c.data.kind == "connection" for c in prod_folder.children)


async def test_group_mode_dual_purpose_node_gets_self_child(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        ifp_node = tree._conn_nodes["ifp"]
        assert ifp_node.data.kind == "connection"
        assert ifp_node.data.conn_name == "ifp"

        ifp_node.expand()  # hybrid node's children are pre-built - no worker involved
        await pilot.pause()
        kinds = [(c.data.kind, c.data.conn_name) for c in ifp_node.children]
        assert ("connection-self", "ifp") in kinds
        assert ("connection", "ifp-gateway") in kinds


async def test_group_mode_leaf_without_siblings_behaves_like_flat(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        # "pg" shares no prefix with anything else, so it's an ordinary leaf.
        pg_node = tree._conn_nodes["pg"]
        assert pg_node.data.kind == "connection"
        pg_node.expand()
        await app.workers.wait_for_complete()
        await pilot.pause()
        labels = [str(c.label) for c in pg_node.children]
        assert any("users" in label for label in labels)


async def test_expanding_self_node_connects_and_loads_schema(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        ifp_node = tree._conn_nodes["ifp"]
        ifp_node.expand()
        await pilot.pause()
        self_node = next(c for c in ifp_node.children if c.data.kind == "connection-self")

        self_node.expand()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.sessions.get("ifp").connected
        labels = [str(c.label) for c in self_node.children]
        assert any("users" in label for label in labels)


async def test_disconnecting_dual_purpose_node_preserves_subgroup_children(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        ifp_node = tree._conn_nodes["ifp"]
        ifp_node.expand()
        await pilot.pause()
        self_node = next(c for c in ifp_node.children if c.data.kind == "connection-self")
        self_node.expand()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.sessions.connect("ifp-gateway")

        tree.select_node(ifp_node)
        await pilot.pause()
        tree.action_disconnect_selected()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not app.sessions.get("ifp").connected
        # the sub-connection and the self-node itself must survive.
        kinds = [(c.data.kind, c.data.conn_name) for c in ifp_node.children]
        assert ("connection-self", "ifp") in kinds
        assert ("connection", "ifp-gateway") in kinds
        # unrelated sibling connection is untouched.
        assert app.sessions.get("ifp-gateway").connected


async def test_connect_disconnect_from_cursor_on_dual_purpose_node(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        ifp_node = tree._conn_nodes["ifp"]
        tree.select_node(ifp_node)
        await pilot.pause()
        assert tree.connection_name_at_cursor() == "ifp"

        tree.action_connect_selected()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.sessions.get("ifp").connected

        tree.action_disconnect_selected()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.sessions.get("ifp").connected


async def test_activating_dual_purpose_node_opens_sql_tab(grouped_registry):
    from dbctl.ui.editor_tab import SqlEditorPane

    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        ifp_node = tree._conn_nodes["ifp"]
        tree.select_node(ifp_node)
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        assert pane.conn_name == "ifp"


async def test_toggling_group_mode_off_restores_flat_list(grouped_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()
        tree.action_toggle_group_mode()
        await pilot.pause()

        assert tree.group_mode is False
        assert {c.data.conn_name for c in tree.root.children} == set(grouped_registry)


async def test_edit_reload_preserves_group_mode(grouped_registry, monkeypatch):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_group_mode()
        await pilot.pause()

        monkeypatch.setattr(
            "dbctl.ui.connection_tree.load_connections", lambda profile=None: grouped_registry
        )
        monkeypatch.setattr("subprocess.run", lambda *a, **k: None)

        class FakeSuspend:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(app, "suspend", lambda: FakeSuspend())
        tree._edit_file()
        await pilot.pause()

        assert tree.group_mode is True
        assert any(c.data.kind == "group" for c in tree.root.children)
