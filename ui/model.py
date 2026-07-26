"""Pure data model for the library UI. No UI dependencies."""

from __future__ import annotations

from pathlib import Path

EXTRAS_SERIES_ID = "EXTRAS"
EXTRAS_SERIES_TITLE = "Extras (local files)"


class SeriesEntry:
    def __init__(self, sid: str, title: str, books: list[dict]):
        self.sid = sid
        self.title = title
        self.books = books                          # sorted by vol number
        self.selected: set[str] = set()             # book ids currently selected

    @property
    def n_selected(self) -> int:
        return len(self.selected)

    @property
    def all_selected(self) -> bool:
        return self.n_selected == len(self.books)

    def toggle_all(self) -> None:
        if self.all_selected:
            self.selected.clear()
        else:
            self.selected = {b["id"] for b in self.books}

    def toggle_book(self, book_id: str) -> None:
        if book_id in self.selected:
            self.selected.discard(book_id)
        else:
            self.selected.add(book_id)

    def select_all_books(self) -> None:
        self.selected = {b["id"] for b in self.books}

    def deselect_all_books(self) -> None:
        self.selected.clear()


def build_entries(grouped: dict[str, dict]) -> list[SeriesEntry]:
    return [
        SeriesEntry(sid, info["title"], info["books"])
        for sid, info in grouped.items()
    ]


def scan_extras(extras_dir: Path) -> list[dict]:
    """Scan *extras_dir* for manually-added EPUBs.

    Returns book dicts compatible with the library ones. Extras carry
    ``extra=True`` and a local ``path``; they have no ``epub_link`` so
    they are never downloaded.
    """
    if not extras_dir.is_dir():
        return []

    books: list[dict] = []
    epubs = sorted(extras_dir.glob("*.epub"), key=lambda p: p.name.lower())
    for n, epub in enumerate(epubs, 1):
        stem = epub.stem
        title = stem.replace("_", " ").replace("-", " ").strip() or stem
        books.append({
            "id": f"extra:{stem}",
            "title": title,
            "short": title,
            "number": n,
            "slug": stem,
            "status": "local",
            "epub_link": None,
            "extra": True,
            "path": str(epub),
        })
    return books


def build_extras_entry(extras_dir: Path) -> SeriesEntry | None:
    """Build the synthetic 'Extras' series entry, or None if empty."""
    books = scan_extras(extras_dir)
    if not books:
        return None
    return SeriesEntry(EXTRAS_SERIES_ID, EXTRAS_SERIES_TITLE, books)


def gather_selected_books(entries: list[SeriesEntry]) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        for b in e.books:
            if b["id"] in e.selected:
                out.append(b)
    return out


def collect_downloadable_books(grouped: dict[str, dict]) -> list[dict]:
    books: list[dict] = []
    for info in grouped.values():
        for bk in info["books"]:
            if bk.get("epub_link"):
                books.append(bk)
    return books
