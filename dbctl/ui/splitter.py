"""A 1-column draggable vertical bar between the connection tree and the
workspace. Complements the app's Ctrl+Left/Ctrl+Right keybindings for
terminals with mouse support."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual import events
from textual.app import RenderResult
from textual.widget import Widget

if TYPE_CHECKING:
    from dbctl.ui.app import DbctlApp


class VerticalSplitter(Widget):
    """Drag to resize the connection tree via `DbctlApp.tree_width`."""

    DEFAULT_CSS = """
    VerticalSplitter {
        width: 1;
        background: $panel;
    }
    VerticalSplitter:hover {
        background: $accent;
    }
    """

    def __init__(self, *, min_width: int, max_width: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._min_width = min_width
        self._max_width = max_width
        self._dragging = False
        self._drag_start_x = 0.0
        self._start_width = 0

    def render(self) -> RenderResult:
        # Widget's default render() falls back to showing the widget's own
        # CSS identifier as text - blank this out, it's a 1-column bar.
        return ""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._drag_start_x = event.screen_x
        self._start_width = cast("DbctlApp", self.app).tree_width
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = int(event.screen_x - self._drag_start_x)
        width = max(self._min_width, min(self._max_width, self._start_width + delta))
        cast("DbctlApp", self.app).tree_width = width

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False
        self.release_mouse()
