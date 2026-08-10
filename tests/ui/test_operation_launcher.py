"""Ctrl+O: searchable operation launcher and its connection-resolution rules."""

from __future__ import annotations

import pytest

from dbctl.config import Operation
from dbctl.ui.app import DbctlApp
from dbctl.ui.connection_tree import ConnectionTree
from dbctl.ui.operation_tab import OperationPane
from dbctl.ui.screens import OperationLauncherScreen


@pytest.fixture()
def find_user_op() -> Operation:
    return Operation.model_validate({"scope": "single", "mode": "fetch", "sql": "SELECT 1", "parameters": []})


async def test_launch_operation_with_none_configured_notifies(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        app.action_launch_operation()
        assert not isinstance(app.screen, OperationLauncherScreen)


async def test_launch_operation_resolves_single_connection_automatically(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    async with app.run_test() as pilot:
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert isinstance(app.screen, OperationLauncherScreen)


async def test_launch_operation_uses_tree_cursor_connection(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    async with app.run_test() as pilot:
        tree = app.query_one(ConnectionTree)
        tree.select_node(tree._conn_nodes["sqlite-test"])
        await pilot.pause()

        app.action_launch_operation()
        await pilot.pause()
        assert isinstance(app.screen, OperationLauncherScreen)

        select = app.screen.query_one("#operation-launcher-select")
        select.value = "find-user"
        await pilot.pause()

        pane = app.query_one(OperationPane)
        assert pane.conn_name == "sqlite-test"
        assert pane.op_name == "find-user"


async def test_launch_operation_uses_active_tab_connection(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        app.action_launch_operation()
        await pilot.pause()
        select = app.screen.query_one("#operation-launcher-select")
        select.value = "find-user"
        await pilot.pause()

        panes = app.query(OperationPane)
        assert len(panes) == 1
        assert panes.first().conn_name == "sqlite-test"


async def test_launch_operation_cancel_button_opens_no_tab(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    async with app.run_test() as pilot:
        app.action_launch_operation()
        await pilot.pause()
        # The Select auto-opens its overlay on mount (for immediate typing),
        # which visually covers the buttons below it - close it first, same
        # as a mouse user would need to before reaching Cancel.
        app.screen.query_one("#operation-launcher-select").expanded = False
        await pilot.pause()
        await pilot.click("#operation-launcher-cancel")
        await pilot.pause()

        assert not isinstance(app.screen, OperationLauncherScreen)
        assert len(app.query(OperationPane)) == 0


async def test_launch_operation_escape_cancels(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    async with app.run_test() as pilot:
        app.action_launch_operation()
        await pilot.pause()
        assert isinstance(app.screen, OperationLauncherScreen)

        # First escape closes the auto-opened Select overlay, the second
        # dismisses the launcher itself.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, OperationLauncherScreen)
        assert len(app.query(OperationPane)) == 0


async def test_launch_operation_with_ambiguous_connection_notifies(stub_registry, find_user_op):
    app = DbctlApp()
    app.operations["find-user"] = find_user_op
    app.connections["other"] = app.connections["sqlite-test"].model_copy(deep=True)
    app.sessions.reload(app.connections)
    async with app.run_test():
        # nothing highlighted in the tree, no active tab, two connections -
        # there's no unambiguous target, so the launcher must not open.
        app.action_launch_operation()
        assert not isinstance(app.screen, OperationLauncherScreen)
