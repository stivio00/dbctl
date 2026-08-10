from __future__ import annotations

import pytest
from textual.widgets import RadioSet, Select, TabbedContent, TabPane

from dbctl.config import Operation
from dbctl.ui.app import DbctlApp
from dbctl.ui.editor_tab import SqlEditorPane
from dbctl.ui.screens import ConfirmScreen, NewTabScreen


@pytest.fixture()
def op(stub_registry):
    return Operation.model_validate({"scope": "single", "mode": "fetch", "sql": "SELECT 1", "parameters": []})


async def test_new_tab_flow_opens_sql_tab_by_default(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.action_new_tab()
        await pilot.pause()
        assert isinstance(app.screen, NewTabScreen)

        app.screen.query_one("#new-tab-connection", Select).value = "sqlite-test"
        await pilot.pause()
        await pilot.click("#new-tab-create")
        await pilot.pause()

        tabbed = app.query_one(TabbedContent)
        panes = tabbed.query(TabPane)
        assert len(panes) == 1
        assert panes.first().get_child_by_type(SqlEditorPane) is not None


async def test_new_tab_flow_opens_operation_tab(stub_registry, op):
    app = DbctlApp()
    app.operations["op1"] = op
    async with app.run_test() as pilot:
        app.action_new_tab()
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, NewTabScreen)
        screen.query_one("#new-tab-connection", Select).value = "sqlite-test"
        screen.query_one(RadioSet).action_toggle_button()  # no-op guard; explicit press below
        # Press the "Operation" radio button directly.
        from textual.widgets import RadioButton

        screen.query_one("#kind-op", RadioButton).value = True
        screen.query_one("#new-tab-operation", Select).value = "op1"
        await pilot.pause()
        await pilot.click("#new-tab-create")
        await pilot.pause()

        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1


async def test_new_tab_cancel_opens_no_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.action_new_tab()
        await pilot.pause()
        await pilot.click("#new-tab-cancel")
        await pilot.pause()

        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 0


async def test_new_tab_with_no_connections_notifies_instead_of_opening_modal(monkeypatch):
    monkeypatch.setattr("dbctl.ui.registry.load_connections", lambda profile=None: {})
    monkeypatch.setattr("dbctl.ui.registry.load_operations", lambda profile=None: {})
    app = DbctlApp()
    async with app.run_test():
        app.action_new_tab()
        assert not isinstance(app.screen, NewTabScreen)


async def test_new_tab_escape_cancels(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.action_new_tab()
        await pilot.pause()
        assert isinstance(app.screen, NewTabScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewTabScreen)
        assert len(app.query(TabPane)) == 0


async def test_confirm_screen_escape_dismisses_false():
    app = DbctlApp()
    async with app.run_test() as pilot:
        result: dict[str, bool] = {}
        app.push_screen(ConfirmScreen("apply?"), lambda ok: result.update(value=ok))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert result == {"value": False}


async def test_confirm_screen_apply_button_dismisses_true():
    app = DbctlApp()
    async with app.run_test() as pilot:
        result: dict[str, bool] = {}
        app.push_screen(ConfirmScreen("apply?"), lambda ok: result.update(value=ok))
        await pilot.pause()
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert result == {"value": True}
