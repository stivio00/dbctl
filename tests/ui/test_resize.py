"""Panel resize: Ctrl+Left/Ctrl+Right keybindings and the draggable
VerticalSplitter both adjust `DbctlApp.tree_width`, which the connection
tree pane's width follows via a reactive watcher."""

from __future__ import annotations

from textual.events import MouseMove

from dbctl.ui.app import DEFAULT_TREE_WIDTH, MAX_TREE_WIDTH, MIN_TREE_WIDTH, TREE_WIDTH_STEP, DbctlApp


async def test_initial_tree_width_is_applied_on_mount(stub_registry):
    app = DbctlApp()
    async with app.run_test():
        assert app.tree_width == DEFAULT_TREE_WIDTH
        tree = app.query_one("#connection-tree")
        assert tree.styles.width is not None
        assert tree.styles.width.value == DEFAULT_TREE_WIDTH


async def test_widen_and_narrow_keybindings(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        app.action_widen_tree()
        await pilot.pause()
        assert app.tree_width == DEFAULT_TREE_WIDTH + TREE_WIDTH_STEP

        app.action_narrow_tree()
        app.action_narrow_tree()
        await pilot.pause()
        assert app.tree_width == DEFAULT_TREE_WIDTH - TREE_WIDTH_STEP

        tree = app.query_one("#connection-tree")
        assert tree.styles.width.value == app.tree_width


async def test_resize_clamps_to_min_and_max(stub_registry):
    app = DbctlApp()
    async with app.run_test() as pilot:
        for _ in range(30):
            app.action_narrow_tree()
        await pilot.pause()
        assert app.tree_width == MIN_TREE_WIDTH

        for _ in range(30):
            app.action_widen_tree()
        await pilot.pause()
        assert app.tree_width == MAX_TREE_WIDTH


async def test_dragging_splitter_resizes_tree(stub_registry):
    app = DbctlApp()
    async with app.run_test(size=(120, 40)) as pilot:
        start_width = app.tree_width
        await pilot.mouse_down("#tree-splitter")
        await pilot.pause()

        target_x = start_width + 20
        await pilot._post_mouse_events([MouseMove], widget=None, offset=(target_x, 5))
        await pilot.pause()
        assert app.tree_width == start_width + 20

        await pilot.mouse_up(widget=None, offset=(target_x, 5))
        await pilot.pause()
        assert app.tree_width == start_width + 20  # unchanged by the release itself


async def test_dragging_splitter_clamps_to_bounds(stub_registry):
    app = DbctlApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.mouse_down("#tree-splitter")
        await pilot.pause()

        await pilot._post_mouse_events([MouseMove], widget=None, offset=(0, 5))
        await pilot.pause()
        assert app.tree_width == MIN_TREE_WIDTH

        await pilot.mouse_up(widget=None, offset=(0, 5))
        await pilot.pause()
