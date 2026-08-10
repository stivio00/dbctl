"""Shared base for tab content that Ctrl+R (or the Run button) executes."""

from __future__ import annotations

from textual.containers import Vertical


class RunnableTab(Vertical):
    """Base class for the SQL editor pane and the operation-launcher pane."""

    def run_tab(self) -> None:
        raise NotImplementedError
