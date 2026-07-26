"""UI abstraction for jnoveldl.

This package provides a frontend-agnostic core (model + abstract `LibraryUI`
+ `LibraryController`) plus concrete backends (`CursesUI`, `PlainUI`). A
future GUI can be added by subclassing `LibraryUI`.
"""

from .base import Action, LibraryUI
from .controller import LibraryController
from .model import (
    SeriesEntry,
    build_entries,
    build_extras_entry,
    collect_downloadable_books,
    gather_selected_books,
    scan_extras,
)
from .registry import get_ui

__all__ = [
    "Action",
    "LibraryUI",
    "LibraryController",
    "SeriesEntry",
    "build_entries",
    "build_extras_entry",
    "collect_downloadable_books",
    "gather_selected_books",
    "scan_extras",
    "get_ui",
]
