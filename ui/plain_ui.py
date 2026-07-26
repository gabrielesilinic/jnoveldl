"""Plain stdlib-only `LibraryUI` for Windows / dumb terminals.

Numbered-menu REPL UI. Uses minimal ANSI styling when stdout is a tty and
NO_COLOR is unset; degrades gracefully otherwise.
"""

from __future__ import annotations

import os
import shutil
import sys

from .base import Action, LibraryUI
from .model import SeriesEntry


def _detect_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Try to enable VT processing on modern consoles; if it fails the
        # escapes will still show literally, so disable in that case.
        try:
            if os.system("") != 0:
                return False
        except Exception:  # pylint: disable=broad-except
            return False
    return True


USE_ANSI = _detect_ansi()


def _style(text: str, code: str) -> str:
    if not USE_ANSI:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _bold(text: str) -> str:
    return _style(text, "1")


def _dim(text: str) -> str:
    return _style(text, "2")


def _green(text: str) -> str:
    return _style(text, "32")


def _yellow(text: str) -> str:
    return _style(text, "33")


def _red(text: str) -> str:
    return _style(text, "31")


def _cyan(text: str) -> str:
    return _style(text, "36")


def _clear_screen() -> None:
    if USE_ANSI:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    else:
        print()
        print("─" * 60)
        print()


def _page_size() -> int:
    try:
        rows = shutil.get_terminal_size((80, 24)).lines
    except Exception:  # pylint: disable=broad-except
        rows = 24
    # leave room for header (3) + footer (4)
    return max(5, rows - 7)


def _prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        return "__quit__"


class PlainUI(LibraryUI):
    """Numbered-menu REPL frontend. Works without curses."""

    HELP_SERIES = (
        "Commands:\n"
        "  <num>          toggle ALL volumes of that series\n"
        "  o <num>        open volume list for that series\n"
        "  a              select all series (all volumes)\n"
        "  A              deselect everything\n"
        "  n / p          next / previous page\n"
        "  d              download selected\n"
        "  c              download + convert to m4b\n"
        "  q              quit\n"
        "  ?              show this help\n"
    )

    HELP_VOLUMES = (
        "Commands:\n"
        "  <num>          toggle that volume\n"
        "  <a-b>          toggle range, e.g. 3-7\n"
        "  a              select all volumes\n"
        "  n              deselect all volumes\n"
        "  e <num>        EXTEND that volume's existing m4b with new EPUB chapters\n"
        "                   (single-book; appends without re-synthesising existing audio)\n"
        "  m <num>        BAKE that volume's existing m4b to an Opus MKA (48 kHz)\n"
        "                   (single-book; optional size-saver for very big files)\n"
        "  next / prev    next / previous page\n"
        "  b / q          back to series list\n"
        "  ?              show this help\n"
    )

    # ── main run loop ────────────────────────────────────────────
    def run(self, entries: list[SeriesEntry]) -> Action:
        page = 0
        while True:
            page_size = _page_size()
            pages = max(1, (len(entries) + page_size - 1) // page_size)
            page = max(0, min(page, pages - 1))

            self._draw_series(entries, page, page_size, pages)
            total_sel = sum(e.n_selected for e in entries)
            cmd = _prompt(
                f"\n[{total_sel} vol(s) selected]  series> "
            )

            if cmd == "__quit__":
                return Action.QUIT
            if not cmd:
                continue

            low = cmd.lower()
            if low in ("q", "quit", "exit"):
                return Action.QUIT
            if low == "d":
                return Action.DOWNLOAD
            if low == "c":
                return Action.CONVERT
            if cmd == "?" or low == "help":
                _clear_screen()
                print(self.HELP_SERIES)
                _prompt("Press Enter to continue …")
                continue
            if low in ("n", "next"):
                page += 1
                continue
            if low in ("p", "prev"):
                page -= 1
                continue
            if cmd == "a":  # select all
                for e in entries:
                    e.select_all_books()
                continue
            if cmd == "A":  # deselect all
                for e in entries:
                    e.deselect_all_books()
                continue

            # open: "o N"
            parts = cmd.split()
            if len(parts) == 2 and parts[0].lower() == "o" and parts[1].isdigit():
                idx = int(parts[1]) - 1
                if 0 <= idx < len(entries):
                    sub_action = self._volume_menu(entries[idx])
                    if sub_action is not None:
                        return sub_action
                else:
                    self._flash(f"No series #{parts[1]}")
                continue

            # bare number: toggle-all on that series
            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(entries):
                    entries[idx].toggle_all()
                else:
                    self._flash(f"No series #{cmd}")
                continue

            self._flash(f"Unknown command: {cmd!r}  (try '?')")

    # ── drawing ──────────────────────────────────────────────────
    def _draw_series(
        self,
        entries: list[SeriesEntry],
        page: int,
        page_size: int,
        pages: int,
    ) -> None:
        _clear_screen()
        title = _bold(_cyan("J-Novel Club Library"))
        print(title)
        print(_dim(f"  {len(entries)} series  ·  page {page + 1}/{pages}"))
        print()

        start = page * page_size
        end = min(start + page_size, len(entries))
        for i in range(start, end):
            e = entries[i]
            if e.all_selected:
                mark = _green("[x]")
            elif e.n_selected > 0:
                mark = _yellow("[~]")
            else:
                mark = "[ ]"
            count = _dim(f"({e.n_selected}/{len(e.books)})")
            print(f"  {i + 1:>3}. {mark} {e.title}  {count}")

        print()
        print(_dim(
            "  <num>=toggle  'o N'=open  a/A=all/none  n/p=page  "
            "d=download  c=convert  q=quit  ?=help"
        ))

    def _flash(self, msg: str) -> None:
        print(_yellow(f"  ! {msg}"))
        _prompt("Press Enter to continue …")

    # ── volume drill-down ────────────────────────────────────────
    def _volume_menu(self, entry: SeriesEntry) -> Action | None:
        """Return an Action to escalate to the top-level loop, else None."""
        page = 0
        while True:
            page_size = _page_size()
            pages = max(1, (len(entry.books) + page_size - 1) // page_size)
            page = max(0, min(page, pages - 1))

            self._draw_volumes(entry, page, page_size, pages)
            cmd = _prompt(
                f"\n[{entry.n_selected}/{len(entry.books)} selected]  "
                f"{entry.title[:30]}> "
            )

            if cmd == "__quit__":
                return None
            if not cmd:
                continue
            low = cmd.lower()
            if low in ("b", "back", "q", "quit", "exit"):
                return None
            if cmd == "?" or low == "help":
                _clear_screen()
                print(self.HELP_VOLUMES)
                _prompt("Press Enter to continue …")
                continue
            if low in ("next",):
                page += 1
                continue
            if low in ("prev",):
                page -= 1
                continue
            if low == "a":
                entry.select_all_books()
                continue
            if low == "n":
                entry.deselect_all_books()
                continue

            # extend/bake single book: "e N" / "m N"
            parts = cmd.split()
            if (
                len(parts) == 2
                and parts[0].lower() in ("e", "m")
                and parts[1].isdigit()
            ):
                idx = int(parts[1]) - 1
                if 0 <= idx < len(entry.books):
                    self.pending_action_book = entry.books[idx]
                    return Action.EXTEND if parts[0].lower() == "e" else Action.BAKE
                self._flash(f"No volume #{parts[1]}")
                continue

            # range "a-b"
            if "-" in cmd:
                a, _, b = cmd.partition("-")
                if a.strip().isdigit() and b.strip().isdigit():
                    lo, hi = int(a), int(b)
                    if lo > hi:
                        lo, hi = hi, lo
                    for i in range(lo - 1, hi):
                        if 0 <= i < len(entry.books):
                            entry.toggle_book(entry.books[i]["id"])
                    continue

            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(entry.books):
                    entry.toggle_book(entry.books[idx]["id"])
                else:
                    self._flash(f"No volume #{cmd}")
                continue

            self._flash(f"Unknown command: {cmd!r}  (try '?')")

    def _draw_volumes(
        self,
        entry: SeriesEntry,
        page: int,
        page_size: int,
        pages: int,
    ) -> None:
        _clear_screen()
        print(_bold(_cyan(entry.title)))
        print(_dim(
            f"  {len(entry.books)} volume(s)  ·  page {page + 1}/{pages}"
        ))
        print()

        start = page * page_size
        end = min(start + page_size, len(entry.books))
        for i in range(start, end):
            bk = entry.books[i]
            mark = _green("[x]") if bk["id"] in entry.selected else "[ ]"
            num = f"Vol {bk['number']:>2}"
            print(f"  {i + 1:>3}. {mark} {num}  {bk['title']}")

        print()
        print(_dim(
            "  <num>=toggle  a-b=range  a=all  n=none  e <num>=extend  m <num>=bake-mka  "
            "next/prev=page  b=back  ?=help"
        ))

    # ── progress hooks: colorize status tags ─────────────────────
    def on_item_result(self, phase: str, status: str, detail: str = "") -> None:
        if not USE_ANSI:
            super().on_item_result(phase, status, detail)
            return
        if phase == "download":
            if status == "ok":
                print(_green(detail or "✓"))
            elif status == "skip":
                print(_dim("skip (already exists)"))
            elif status == "no-link":
                print(_yellow("✗ no EPUB link"))
            elif status == "fail":
                print(_red(f"FAILED ({detail})"))
            else:
                print(status)
        else:
            if status == "ok":
                print("    → " + _green(f"{detail} ✓" if detail else "✓"))
            elif status == "skip":
                what = "mka" if phase == "bake" else "m4b"
                print("    → " + _dim(f"skip ({what} already exists)"))
            elif status == "no-m4b":
                print("    → " + _dim("skip (no existing m4b — run convert first)"))
            elif status == "no-epub":
                print("    → " + _dim("skip (epub not found)"))
            elif status == "fail":
                print("    → " + _red(f"FAILED ({detail})"))
            else:
                print(f"    → {status}")
