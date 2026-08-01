"""dbctl - generic CLI to monitor, control, and administer multiple databases."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("dbctl")
except PackageNotFoundError:  # source checkout without install
    __version__ = "0.0.0+unknown"
