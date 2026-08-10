from __future__ import annotations

import pytest
from textual.widgets import DataTable, Input

from dbctl.audit import read as read_audit
from dbctl.config import Operation
from dbctl.ui.app import DbctlApp
from dbctl.ui.operation_tab import OperationPane


@pytest.fixture()
def add_user_op() -> Operation:
    return Operation.model_validate(
        {
            "description": "add a user",
            "scope": "single",
            "mode": "execute",
            "confirm": False,
            "parameters": [
                {"name": "name", "type": "string", "required": True, "position": 1},
            ],
            "sql": "INSERT INTO users (name) VALUES ($name)",
        }
    )


@pytest.fixture()
def list_users_op() -> Operation:
    return Operation.model_validate(
        {
            "description": "list users",
            "scope": "single",
            "mode": "fetch",
            "parameters": [],
            "sql": "SELECT * FROM users ORDER BY id",
        }
    )


async def test_operation_tab_fetch_populates_results(stub_registry, list_users_op):
    app = DbctlApp()
    app.operations["list-users"] = list_users_op
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_operation_tab("sqlite-test", "list-users")
        await pilot.pause()

        app.action_run_tab()
        await pilot.pause()

        pane = app.query_one(OperationPane)
        table = pane.query_one("#results-table", DataTable)
        assert table.row_count == 2


async def test_operation_tab_execute_writes_and_audits(stub_registry, add_user_op):
    app = DbctlApp()
    app.operations["add-user"] = add_user_op
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_operation_tab("sqlite-test", "add-user")
        await pilot.pause()

        pane = app.query_one(OperationPane)
        name_input = pane.query_one("#param-name", Input)
        name_input.value = "carol"

        app.action_run_tab()
        await pilot.pause()

        table = pane.query_one("#results-table", DataTable)
        row = list(table.rows.keys())[0]
        cell = table.get_cell(row, next(iter(table.columns.keys())))
        assert "row(s) affected" in cell

    entries = read_audit(None, limit=10)
    assert entries[-1]["operation"] == "add-user"
    assert entries[-1]["status"] == "ok"


async def test_operation_tab_respects_read_only_safety(stub_registry, add_user_op):
    app = DbctlApp()
    app.operations["add-user"] = add_user_op
    app.connections["sqlite-test"].safety.read_only = True
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_operation_tab("sqlite-test", "add-user")
        await pilot.pause()

        pane = app.query_one(OperationPane)
        pane.query_one("#param-name", Input).value = "dave"

        app.action_run_tab()
        await pilot.pause()

        table = pane.query_one("#results-table", DataTable)
        row = list(table.rows.keys())[0]
        cell = table.get_cell(row, next(iter(table.columns.keys())))
        assert "read-only" in cell


async def test_operation_tab_survives_connection_removed_from_registry(stub_registry, add_user_op):
    """A tab bound to a connection that's since been removed must show a
    message, not crash with a KeyError from the now-empty SessionManager."""
    app = DbctlApp()
    app.operations["add-user"] = add_user_op
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_operation_tab("sqlite-test", "add-user")
        await pilot.pause()

        app.sessions.reload({})  # simulates the connection disappearing

        app.action_run_tab()  # must not raise
        await pilot.pause()

        pane = app.query_one(OperationPane)
        table = pane.query_one("#results-table", DataTable)
        row = list(table.rows.keys())[0]
        cell = table.get_cell(row, next(iter(table.columns.keys())))
        assert "no longer exists" in cell
