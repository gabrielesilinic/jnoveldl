"""Curses-based `LibraryUI` implementation.

UX is preserved 1:1 from the original tui.py implementation.
"""

from __future__ import annotations

import curses

from .base import Action, LibraryUI
from .model import SeriesEntry


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


def _draw_bar(win, y: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h:
        return
    win.move(y, 0)
    win.clrtoeol()
    win.addnstr(y, 0, text, w - 1, attr)


class CursesUI(LibraryUI):
    """Full-screen curses frontend."""

    def run(self, entries: list[SeriesEntry]) -> Action:
        result = curses.wrapper(lambda win: self._series_list_view(win, entries))
        return result

    # ── series list view ─────────────────────────────────────────
    def _draw_series_list(
        self,
        win,
        entries: list[SeriesEntry],
        cursor: int,
        scroll: int,
        total_sel: int,
    ) -> None:
        win.erase()
        h, w = win.getmaxyx()
        header = " J-Novel Club Library "
        footer = " ↑↓:move  Space:toggle  Enter:volumes(→e:extend m:mka)  d:download  c:dl+m4b  q:quit "
        status = f" {total_sel} volume(s) selected "

        _draw_bar(win, 0, header.center(w - 1), curses.A_BOLD | curses.A_REVERSE)

        list_top = 2
        list_bot = h - 3
        visible = list_bot - list_top

        for i in range(visible):
            idx = scroll + i
            if idx >= len(entries):
                break
            e = entries[idx]
            y = list_top + i

            if e.all_selected:
                mark = "[✓]"
            elif e.n_selected > 0:
                mark = "[-]"
            else:
                mark = "[ ]"

            count = f"({e.n_selected}/{len(e.books)})"
            line = f" {mark} {e.title}  {count}"

            attr = curses.A_NORMAL
            if idx == cursor:
                attr = curses.A_REVERSE

            win.move(y, 0)
            win.clrtoeol()
            win.addnstr(y, 0, line, w - 1, attr)

        _draw_bar(win, h - 2, status.center(w - 1), curses.A_BOLD)
        _draw_bar(win, h - 1, footer.center(w - 1), curses.A_DIM)
        win.refresh()

    def _series_list_view(self, win, entries: list[SeriesEntry]) -> Action:
        curses.curs_set(0)
        cursor = 0
        scroll = 0

        while True:
            h, _w = win.getmaxyx()
            visible = max(1, h - 5)
            total_sel = sum(e.n_selected for e in entries)

            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + visible:
                scroll = cursor - visible + 1

            self._draw_series_list(win, entries, cursor, scroll, total_sel)
            key = win.getch()

            if key == curses.KEY_UP or key == ord("k"):
                cursor = _clamp(cursor - 1, 0, len(entries) - 1)
            elif key == curses.KEY_DOWN or key == ord("j"):
                cursor = _clamp(cursor + 1, 0, len(entries) - 1)
            elif key == ord(" "):
                entries[cursor].toggle_all()
            elif key in (curses.KEY_ENTER, 10, 13):
                sub = self._volume_list_view(win, entries[cursor])
                if sub is not None:
                    return sub
            elif key == ord("d") or key == ord("D"):
                return Action.DOWNLOAD
            elif key == ord("c") or key == ord("C"):
                return Action.CONVERT
            elif key == ord("q") or key == ord("Q"):
                return Action.QUIT

    # ── volume list view ─────────────────────────────────────────
    def _draw_volume_list(
        self, win, entry: SeriesEntry, cursor: int, scroll: int
    ) -> None:
        win.erase()
        h, w = win.getmaxyx()
        header = f" {entry.title} "
        footer = " ↑↓:move  Space:toggle  a:all  n:none  e:extend  m:bake-mka  Esc:back "
        status = f" {entry.n_selected}/{len(entry.books)} selected "

        _draw_bar(win, 0, header.center(w - 1), curses.A_BOLD | curses.A_REVERSE)

        list_top = 2
        list_bot = h - 3
        visible = list_bot - list_top

        for i in range(visible):
            idx = scroll + i
            if idx >= len(entry.books):
                break
            bk = entry.books[idx]
            y = list_top + i

            mark = "[✓]" if bk["id"] in entry.selected else "[ ]"
            line = f" {mark} Vol {bk['number']:>2}  {bk['title']}"

            attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
            win.move(y, 0)
            win.clrtoeol()
            win.addnstr(y, 0, line, w - 1, attr)

        _draw_bar(win, h - 2, status.center(w - 1), curses.A_BOLD)
        _draw_bar(win, h - 1, footer.center(w - 1), curses.A_DIM)
        win.refresh()

    def _volume_list_view(self, win, entry: SeriesEntry) -> Action | None:
        """Return an Action to escalate to the series-list loop, else None."""
        cursor = 0
        scroll = 0

        while True:
            h, _w = win.getmaxyx()
            visible = max(1, h - 5)

            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + visible:
                scroll = cursor - visible + 1

            self._draw_volume_list(win, entry, cursor, scroll)
            key = win.getch()

            if key == curses.KEY_UP or key == ord("k"):
                cursor = _clamp(cursor - 1, 0, len(entry.books) - 1)
            elif key == curses.KEY_DOWN or key == ord("j"):
                cursor = _clamp(cursor + 1, 0, len(entry.books) - 1)
            elif key == ord(" "):
                entry.toggle_book(entry.books[cursor]["id"])
            elif key == ord("a") or key == ord("A"):
                entry.select_all_books()
            elif key == ord("n") or key == ord("N"):
                entry.deselect_all_books()
            elif key == ord("e") or key == ord("E"):
                self.pending_action_book = entry.books[cursor]
                return Action.EXTEND
            elif key == ord("m") or key == ord("M"):
                self.pending_action_book = entry.books[cursor]
                return Action.BAKE
            elif key == 27:  # Esc
                return None
