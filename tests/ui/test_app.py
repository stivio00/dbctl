from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import TabbedContent, TabPane  # noqa: E402

from dbctl.ui.app import DbctlApp  # noqa: E402


async def test_multiple_tabs_can_be_open_at_once(stub_registry):
    """Each tab's pane reuses widget ids like `#sql-input` / `#results-table`
    scoped to its own subtree - this guards against Textual's duplicate-id
    mount check (which only applies to siblings) rejecting a second open tab."""
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        app.open_sql_tab("sqlite-test")
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
