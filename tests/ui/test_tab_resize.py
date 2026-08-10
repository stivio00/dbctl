"""Per-tab editor/results split (Ctrl+Up/Ctrl+Down + draggable
HorizontalSplitter) and the compact run/close icon toolbar shared by
`RunnableTab` subclasses."""

from __future__ import annotations

from textual.events import MouseMove
from textual.widgets import Button, DataTable, TabbedContent, TabPane

from dbctl.ui.app import DbctlApp
from dbctl.ui.editor_tab import SqlEditorPane
from dbctl.ui.tabs import DEFAULT_EDITOR_HEIGHT, EDITOR_HEIGHT_STEP, MAX_EDITOR_HEIGHT, MIN_EDITOR_HEIGHT


async def test_editor_height_initial_value_applied(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)
        assert pane.editor_height == DEFAULT_EDITOR_HEIGHT
        assert pane.query_one("#editor-area").styles.height.value == DEFAULT_EDITOR_HEIGHT


async def test_grow_and_shrink_keybindings_resize_active_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()

        app.action_grow_editor()
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)
        assert pane.editor_height == DEFAULT_EDITOR_HEIGHT + EDITOR_HEIGHT_STEP

        app.action_shrink_editor()
        app.action_shrink_editor()
        await pilot.pause()
        assert pane.editor_height == DEFAULT_EDITOR_HEIGHT - EDITOR_HEIGHT_STEP
        assert pane.query_one("#editor-area").styles.height.value == pane.editor_height


async def test_resize_clamps_to_bounds(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)

        for _ in range(60):
            app.action_shrink_editor()
        await pilot.pause()
        assert pane.editor_height == MIN_EDITOR_HEIGHT

        for _ in range(60):
            app.action_grow_editor()
        await pilot.pause()
        assert pane.editor_height == MAX_EDITOR_HEIGHT


async def test_resize_with_no_tabs_open_is_a_no_op(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        app.action_grow_editor()  # must not raise
        app.action_shrink_editor()


async def test_each_tab_keeps_its_own_editor_height(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        app.action_grow_editor()
        app.action_grow_editor()
        await pilot.pause()

        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        panes = app.query(SqlEditorPane)
        assert panes[0].editor_height == DEFAULT_EDITOR_HEIGHT + 2 * EDITOR_HEIGHT_STEP
        assert panes[1].editor_height == DEFAULT_EDITOR_HEIGHT


async def test_dragging_tab_splitter_resizes_editor_area(stub_registry):
    app = DbctlApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        pane = app.query_one(SqlEditorPane)
        start_height = pane.editor_height

        await pilot.mouse_down("#tab-splitter")
        await pilot.pause()
        target_y = start_height + 4
        await pilot._post_mouse_events([MouseMove], widget=pane, offset=(10, target_y))
        await pilot.pause()
        assert pane.editor_height == start_height + 4

        await pilot.mouse_up(widget=pane, offset=(10, target_y))
        await pilot.pause()


async def test_toolbar_run_button_executes_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.sessions.connect("sqlite-test")
        app.open_sql_tab("sqlite-test", sql="SELECT * FROM users")
        await pilot.pause()

        await pilot.click("#run-button")
        await pilot.pause()

        table = app.query_one("#results-table", DataTable)
        assert table.row_count == 2


async def test_toolbar_close_button_closes_the_tab(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert len(tabbed.query(TabPane)) == 1

        await pilot.click("#close-button")
        await pilot.pause()
        assert len(tabbed.query(TabPane)) == 0


async def test_toolbar_buttons_are_compact_icons(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.open_sql_tab("sqlite-test")
        await pilot.pause()
        run_button = app.query_one("#run-button", Button)
        close_button = app.query_one("#close-button", Button)
        assert run_button.compact
        assert close_button.compact
