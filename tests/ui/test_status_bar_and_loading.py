"""Status bar text (row counts / duration) and the loading indicator shown
while a SQL/operation run is in flight - both depend on execution running
in a background worker (`@work(thread=True)`), since Textual can't paint
anything, including a spinner, while the main thread is blocked."""

from __future__ import annotations

from textual.widgets import DataTable, LoadingIndicator, Static, TextArea

from dbctl.config import Operation
from dbctl.ui.app import DbctlApp
from dbctl.ui.editor_tab import SqlEditorPane
from dbctl.ui.operation_tab import OperationPane


def _status(pane) -> str:
    return str(pane.query_one("#status-bar", Static).content)


async def test_sql_run_shows_loading_then_hides_it(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test", sql="SELECT * FROM users")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)

        app.action_run_tab()
        # Checked before awaiting worker completion - this is the
        # synchronous part of run_tab(), so it deterministically captures
        # the in-flight state (no race with the background thread).
        assert pane.running is True
        assert pane.query_one("#run-loading", LoadingIndicator).display is True
        assert pane.query_one("#results-table", DataTable).display is False
        assert _status(pane) == "running…"

        await app.workers.wait_for_complete()
        await pilot.pause()

        assert pane.running is False
        assert pane.query_one("#run-loading", LoadingIndicator).display is False
        assert pane.query_one("#results-table", DataTable).display is True


async def test_sql_fetch_status_bar_shows_row_count_and_duration(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test", sql="SELECT * FROM users")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)

        app.action_run_tab()
        await app.workers.wait_for_complete()
        await pilot.pause()

        status = _status(pane)
        assert "2 row(s)" in status
        assert "ms" in status


async def test_sql_write_status_bar_shows_rows_affected(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)
        pane.query_one("#sql-input", TextArea).text = "DELETE FROM users WHERE name = 'alice'"

        app.action_run_tab()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "row(s) affected" in _status(pane)


async def test_sql_error_status_bar(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test", sql="SELECT * FROM no_such_table")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)

        app.action_run_tab()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert _status(pane).startswith("error")


async def test_double_run_while_running_is_ignored(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test", sql="SELECT * FROM users")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)

        app.action_run_tab()
        assert pane.running is True
        app.action_run_tab()  # must be a no-op - pane is already running
        app.action_run_tab()

        await app.workers.wait_for_complete()
        await pilot.pause()

        assert pane.running is False
        table = pane.query_one("#results-table", DataTable)
        assert table.row_count == 2  # not tripled by redundant runs


async def test_operation_run_shows_loading_and_status(stub_registry):
    op = Operation.model_validate(
        {"scope": "single", "mode": "fetch", "sql": "SELECT * FROM users", "parameters": []}
    )
    app = DbctlApp()
    app.operations["list-users"] = op
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_operation_tab("sqlite-test", "list-users")
        await pilot.pause()
        pane = app.query_one(OperationPane)

        app.action_run_tab()
        assert pane.running is True
        assert pane.query_one("#run-loading", LoadingIndicator).display is True
        assert _status(pane) == "running…"

        await app.workers.wait_for_complete()
        await pilot.pause()

        assert pane.running is False
        assert "2 row(s)" in _status(pane)
