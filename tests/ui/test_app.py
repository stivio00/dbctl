from __future__ import annotations

from textual.widgets import TabbedContent, TabPane

from dbctl.ui.app import DbctlApp
from dbctl.ui.connection_tree import ConnectionActivated, TableActivated
from dbctl.ui.editor_tab import SqlEditorPane


async def test_multiple_tabs_can_be_open_at_once(stub_registry):
    """Each tab's pane reuses widget ids like `#sql-input` / `#results-table`
    scoped to its own subtree - this guards against Textual's duplicate-id
    mount check (which only applies to siblings) rejecting a second open tab.
    The second call passes reuse=False (the Ctrl+N path) because identical
    tree-driven tabs now dedupe instead of stacking."""
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        app.open_sql_tab("sqlite-test", reuse=False)
        await pilot.pause()

        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 2


async def test_close_tab_removes_active_pane(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1

        app.action_close_tab()
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 0

        # closing with no tabs open must not raise
        app.action_close_tab()
        await pilot.pause()


async def test_run_tab_with_no_open_tabs_is_a_no_op(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        app.action_run_tab()  # must not raise


# --------------------------------------------------------------------------- #
# tree activation dedupes tabs instead of stacking duplicates
# --------------------------------------------------------------------------- #
async def test_connection_activation_reuses_scratch_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app._open_default_tab(ConnectionActivated("sqlite-test"))
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1
        first = tabbed.active

        app._open_default_tab(ConnectionActivated("sqlite-test"))
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 1
        assert tabbed.active == first


async def test_table_activation_reuses_same_table_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1
        first = tabbed.active

        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 1
        assert tabbed.active == first

        # a different table still opens its own tab
        app._open_table_tab(TableActivated("sqlite-test", None, "active_users"))
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 2


async def test_table_tab_and_scratch_tab_are_distinct(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app._open_default_tab(ConnectionActivated("sqlite-test"))
        await pilot.pause()
        # activating a table is a different intent than the scratch tab
        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        assert len(app.query_one(TabbedContent).query(TabPane)) == 2


async def test_reopen_after_close_opens_a_new_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1

        app.action_close_tab()
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 0

        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 1


async def test_table_tab_target_survives_edited_sql(stub_registry):
    """Dedupe keys on the tab's origin (conn + table), not its current SQL:
    even after editing the SQL, activating the same table focuses that tab."""
    app = DbctlApp()
    async with app.run_test() as pilot:
        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        editor = app.query_one(SqlEditorPane).query_one("#sql-input")
        editor.text = "SELECT COUNT(*) FROM users;"

        app._open_table_tab(TableActivated("sqlite-test", None, "users"))
        await pilot.pause()
        assert len(app.query_one(TabbedContent).query(TabPane)) == 1
        assert editor.text == "SELECT COUNT(*) FROM users;"
