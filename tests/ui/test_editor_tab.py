from __future__ import annotations

from textual.widgets import DataTable, TextArea

from dbctl.audit import read as read_audit
from dbctl.ui.app import DbctlApp
from dbctl.ui.editor_tab import SqlEditorPane, is_write_statement


def test_is_write_statement_detects_select_as_read_only():
    assert not is_write_statement("SELECT * FROM users")
    assert not is_write_statement("  -- a comment\n  WITH t AS (SELECT 1) SELECT * FROM t")
    assert is_write_statement("UPDATE users SET name = 'x'")
    assert is_write_statement("DELETE FROM users")


async def test_sql_tab_run_populates_results_table(stub_registry, tmp_path):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        text_area = pane.query_one("#sql-input", TextArea)
        text_area.text = "SELECT * FROM users ORDER BY id"

        app.action_run_tab()
        await pilot.pause()

        table = pane.query_one("#results-table", DataTable)
        assert table.row_count == 2
        first_column = next(iter(table.columns.values()))
        assert str(first_column.label) == "id"

    entries = read_audit(None, limit=10)
    assert entries[-1]["connection"] == "sqlite-test"
    assert entries[-1]["mode"] == "ui-sql"
    assert entries[-1]["status"] == "ok"


async def test_sql_tab_shows_error_inline_on_bad_sql(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        text_area = pane.query_one("#sql-input", TextArea)
        text_area.text = "SELECT * FROM no_such_table"

        app.action_run_tab()
        await pilot.pause()

        table = pane.query_one("#results-table", DataTable)
        assert table.row_count == 1  # single error message row, not a crash


async def test_sql_tab_without_connect_shows_hint(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        app.action_run_tab()
        await pilot.pause()

        table = pane.query_one("#results-table", DataTable)
        row = list(table.rows.keys())[0]
        cell = table.get_cell(row, list(table.columns.keys())[0])
        assert "not connected" in cell


async def test_sql_tab_survives_connection_removed_from_registry(stub_registry):
    """A tab bound to a connection that's since been removed (e.g. via the
    tree's 'e' edit-connections.yaml action) must show a message, not crash
    with a KeyError from the now-empty SessionManager."""
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        app.sessions.reload({})  # simulates the connection disappearing

        app.action_run_tab()  # must not raise
        await pilot.pause()

        pane = app.query_one(SqlEditorPane)
        table = pane.query_one("#results-table", DataTable)
        row = list(table.rows.keys())[0]
        cell = table.get_cell(row, list(table.columns.keys())[0])
        assert "no longer exists" in cell
