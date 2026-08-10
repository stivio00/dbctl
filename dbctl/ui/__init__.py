"""Interactive Textual UI for dbctl (`dbctl ui`).

This package only *consumes* the existing loaders/executors
(``dbctl.connections``, ``dbctl.operations``, ``dbctl.tunnels``, ``dbctl.db``,
``dbctl.execute``, ``dbctl.audit``) - it does not change their behavior.
"""

from __future__ import annotations
