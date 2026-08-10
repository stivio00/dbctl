"""Small modal screens: confirm-before-apply, and the new-tab chooser."""

from __future__ import annotations

from collections.abc import Mapping

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RadioButton, RadioSet, Select


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation modal - the UI equivalent of ``confirm_or_abort``."""

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._prompt, id="confirm-prompt")
            with Horizontal():
                yield Button("Apply", variant="warning", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class NewTabScreen(ModalScreen[tuple[str, str, str | None] | None]):
    """Picks a tab kind + connection (+ operation, if applicable).

    Dismisses with ``("sql", conn_name, None)``, ``("operation", conn_name,
    op_name)``, or ``None`` if cancelled.
    """

    def __init__(self, connection_names: list[str], single_scope_ops: Mapping[str, object]) -> None:
        super().__init__()
        self._connection_names = connection_names
        self._op_names = list(single_scope_ops)

    def compose(self) -> ComposeResult:
        with Vertical(id="new-tab-dialog"):
            yield Label("New tab")
            with RadioSet(id="new-tab-kind"):
                yield RadioButton("SQL editor", value=True, id="kind-sql")
                yield RadioButton("Operation", id="kind-op")
            yield Select(
                ((name, name) for name in self._connection_names),
                id="new-tab-connection",
                prompt="connection",
            )
            yield Select(
                ((name, name) for name in self._op_names),
                id="new-tab-operation",
                prompt="operation (for the Operation kind)",
            )
            with Horizontal():
                yield Button("Create", variant="primary", id="new-tab-create")
                yield Button("Cancel", id="new-tab-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-tab-cancel":
            self.dismiss(None)
            return
        if event.button.id != "new-tab-create":
            return
        conn_select = self.query_one("#new-tab-connection", Select)
        if conn_select.is_blank():
            self.app.bell()
            return
        conn_name = str(conn_select.value)

        kind_set = self.query_one("#new-tab-kind", RadioSet)
        is_sql = kind_set.pressed_button is None or kind_set.pressed_button.id == "kind-sql"
        if is_sql:
            self.dismiss(("sql", conn_name, None))
            return

        op_select = self.query_one("#new-tab-operation", Select)
        if op_select.is_blank():
            self.app.bell()
            return
        self.dismiss(("operation", conn_name, str(op_select.value)))
