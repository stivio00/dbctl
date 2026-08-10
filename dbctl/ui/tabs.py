"""Shared base for tab content that Ctrl+R (or the toolbar's Run icon) executes.

Owns the resizable split between the editor/form area (subclass-supplied)
and the shared results `DataTable` below it - `editor_height` is a
reactive so each open tab keeps its own size independently. Also owns the
small icon toolbar (run / close), the status line (row count / rows
affected / duration shown after a run), and the loading indicator swapped
in for the results table while a run is in flight - subclasses drive
`show_loading`/`hide_loading`/`set_status` around their own `@work(thread=True)`
execution (see `editor_tab.py` / `operation_tab.py`); nothing here should
run blocking I/O in the main (UI) thread.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Button, DataTable, LoadingIndicator, Static

from dbctl.ui.splitter import HorizontalSplitter

if TYPE_CHECKING:
    from dbctl.ui.app import DbctlApp

MIN_EDITOR_HEIGHT = 3
MAX_EDITOR_HEIGHT = 60
DEFAULT_EDITOR_HEIGHT = 12
EDITOR_HEIGHT_STEP = 2


class RunnableTab(Vertical):
    """Base class for the SQL editor pane and the operation-launcher pane."""

    conn_name: str
    editor_height: reactive[int] = reactive(DEFAULT_EDITOR_HEIGHT)
    running: reactive[bool] = reactive(False)

    def compose_editor(self) -> ComposeResult:
        """Subclasses yield their header + editor/form widgets here (no toolbar)."""
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-area"):
            yield from self.compose_editor()
            with Horizontal(classes="pane-toolbar"):
                yield Button("▶", id="run-button", variant="primary", compact=True, tooltip="Run (Ctrl+R)")
                yield Button("✕", id="close-button", compact=True, tooltip="Close tab (Ctrl+W)")
        yield HorizontalSplitter(
            self, min_value=MIN_EDITOR_HEIGHT, max_value=MAX_EDITOR_HEIGHT, id="tab-splitter"
        )
        yield LoadingIndicator(id="run-loading")
        yield DataTable(id="results-table")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.query_one("#run-loading", LoadingIndicator).display = False

    def watch_editor_height(self, height: int) -> None:
        with contextlib.suppress(NoMatches):  # not mounted yet - re-fires once it is
            self.query_one("#editor-area").styles.height = height

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-button":
            self.run_tab()
        elif event.button.id == "close-button":
            cast("DbctlApp", self.app).action_close_tab()

    def show_loading(self) -> None:
        self.running = True
        self.query_one("#run-loading", LoadingIndicator).display = True
        self.query_one("#results-table", DataTable).display = False

    def hide_loading(self) -> None:
        self.running = False
        self.query_one("#run-loading", LoadingIndicator).display = False
        self.query_one("#results-table", DataTable).display = True

    def set_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    def run_tab(self) -> None:
        raise NotImplementedError
