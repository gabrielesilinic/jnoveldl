"""Backend registry and auto-selection."""

from __future__ import annotations

import importlib.util
import sys

from .base import LibraryUI


def _curses_available() -> bool:
    if sys.platform == "win32":
        # Standard library curses is not available on Windows without
        # the optional `windows-curses` package — we deliberately don't
        # require it.
        return importlib.util.find_spec("_curses") is not None
    return importlib.util.find_spec("curses") is not None


def get_ui(name: str | None = None) -> type[LibraryUI]:
    """Resolve a backend name to a `LibraryUI` subclass.

    `name` may be 'curses', 'plain', 'auto' (or None / empty for auto).
    """
    if not name or name == "auto":
        if sys.platform == "win32" or not _curses_available():
            name = "plain"
        else:
            name = "curses"

    if name == "curses":
        from .curses_ui import CursesUI
        return CursesUI
    if name == "plain":
        from .plain_ui import PlainUI
        return PlainUI

    raise ValueError(f"Unknown UI backend: {name!r}")
