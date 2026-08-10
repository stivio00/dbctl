from __future__ import annotations

from dbctl.ui.app import DbctlApp
from dbctl.ui.connection_tree import ConnectionTree


async def test_tree_renders_configured_connections(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        tree = app.query_one(ConnectionTree)
        assert set(tree._conn_nodes) == set(stub_registry)
        node = tree._conn_nodes["sqlite-test"]
        assert "sqlite-test" in str(node.label)
        assert "direct" in str(node.label)


async def test_connect_and_disconnect_updates_indicator(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        assert not app.sessions.get("sqlite-test").connected
        session = app.sessions.connect("sqlite-test")
        assert session.connected
        assert session.error is None

        app.sessions.disconnect("sqlite-test")
        assert not app.sessions.get("sqlite-test").connected


async def test_expanding_connection_node_auto_connects_and_lists_tables(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)

        assert app.sessions.get("sqlite-test").connected
        labels = [str(c.label) for c in node.children]
        assert any("users" in label for label in labels)


async def test_simple_mode_table_expands_to_columns(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        assert tree.view_mode == "simple"
        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)

        table_node = node.children[0]
        table_node.expand()
        await pilot.pause(0.2)
        labels = [str(c.label) for c in table_node.children]
        assert any("id" in label for label in labels)
        assert any("name" in label for label in labels)


async def test_reexpanding_does_not_duplicate_children(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)
        first_count = len(node.children)

        node.collapse()
        await pilot.pause(0.05)
        node.expand()
        await pilot.pause(0.2)
        assert len(node.children) == first_count


async def test_toggle_view_mode_switches_to_schema_grouping(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.action_toggle_view_mode()
        assert tree.view_mode == "normal"
        await pilot.pause()

        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)
        schema_labels = [str(c.label) for c in node.children]
        assert schema_labels  # at least one schema (sqlite: "main")

        schema_node = node.children[0]
        schema_node.expand()
        await pilot.pause(0.2)
        group_labels = [str(c.label) for c in schema_node.children]
        assert any("Tables" in label for label in group_labels)
        assert any("Views" in label for label in group_labels)


async def test_activating_table_node_opens_prefilled_sql_tab(stub_registry):
    from textual.widgets import TextArea

    from dbctl.ui.editor_tab import SqlEditorPane

    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)
        table_node = node.children[0]

        tree.select_node(table_node)
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        text = pane.query_one("#sql-input", TextArea).text
        assert "users" in text


async def test_connect_disconnect_work_from_a_nested_cursor_position(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        node = tree._conn_nodes["sqlite-test"]
        node.expand()
        await pilot.pause(0.2)
        table_node = node.children[0]
        tree.select_node(table_node)
        await pilot.pause()

        assert tree._connection_name_at_cursor() == "sqlite-test"
        tree.action_disconnect_selected()
        assert not app.sessions.get("sqlite-test").connected
