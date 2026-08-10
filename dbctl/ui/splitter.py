"""Draggable splitter bars: a vertical one between the connection tree and
the workspace, and a horizontal one between a tab's editor/form area and
its results table. Both complement keybindings on `DbctlApp` for terminals
without mouse support.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual import events
from textual.app import RenderResult
from textual.widget import Widget

if TYPE_CHECKING:
    from dbctl.ui.app import DbctlApp
    from dbctl.ui.tabs import RunnableTab


class _DraggableSplitter(Widget):
    """Shared drag bookkeeping; subclasses supply the axis and the getter/setter."""

    def __init__(self, *, min_value: int, max_value: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._min_value = min_value
        self._max_value = max_value
        self._dragging = False
        self._drag_start = 0.0
        self._start_value = 0

    def render(self) -> RenderResult:
        # Widget's default render() falls back to showing the widget's own
        # CSS identifier as text - blank this out, it's a thin bar.
        return ""

    def _get_value(self) -> int:
        raise NotImplementedError

    def _set_value(self, value: int) -> None:
        raise NotImplementedError

    def _event_pos(self, event: events.MouseEvent) -> float:
        raise NotImplementedError

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._drag_start = self._event_pos(event)
        self._start_value = self._get_value()
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = int(self._event_pos(event) - self._drag_start)
        self._set_value(max(self._min_value, min(self._max_value, self._start_value + delta)))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False
        self.release_mouse()


class VerticalSplitter(_DraggableSplitter):
    """Drag left/right to resize the connection tree via `DbctlApp.tree_width`."""

    DEFAULT_CSS = """
    VerticalSplitter {
        width: 1;
        background: $panel;
    }
    VerticalSplitter:hover {
        background: $accent;
    }
    """

    def _event_pos(self, event: events.MouseEvent) -> float:
        return event.screen_x

    def _get_value(self) -> int:
        return cast("DbctlApp", self.app).tree_width

    def _set_value(self, value: int) -> None:
        cast("DbctlApp", self.app).tree_width = value


class HorizontalSplitter(_DraggableSplitter):
    """Drag up/down to resize a tab's editor/form area via `RunnableTab.editor_height`."""

    DEFAULT_CSS = """
    HorizontalSplitter {
        height: 1;
        background: $panel;
    }
    HorizontalSplitter:hover {
        background: $accent;
    }
    """

    def __init__(self, pane: RunnableTab, *, min_value: int, max_value: int, **kwargs: Any) -> None:
        super().__init__(min_value=min_value, max_value=max_value, **kwargs)
        self._pane = pane

    def _event_pos(self, event: events.MouseEvent) -> float:
        return event.screen_y

    def _get_value(self) -> int:
        return self._pane.editor_height

    def _set_value(self, value: int) -> None:
        self._pane.editor_height = value
