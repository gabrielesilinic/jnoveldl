#!/usr/bin/env python3
"""jnoveldl — curses TUI for browsing and downloading your J-Novel Club library.

Series list:
  ↑/↓       navigate            Space  toggle all volumes in series
  Enter      drill into volumes  d      download selected
  c          download + convert to m4b
  q          quit

Volume list (inside a series):
  ↑/↓       navigate            Space  toggle volume
  a          select all          n      deselect all
  Esc        back to series
"""

import curses
import argparse
import sys
import time
from pathlib import Path

from bookconvert import convert_epub_to_m4b
from jnoveldl import (
    JNCApi,
    bake_credentials_from_keyring,
    clear_credentials,
    download_epub,
    get_stored_credentials,
    is_keyring_available,
    parse_library,
    prompt_credentials,
    store_credentials,
)

DOWNLOAD_DIR = Path("downloads")
AUDIOBOOK_DIR = Path("audiobooks")
DOWNLOAD_DELAY = 2        # seconds between consecutive downloads


def _collect_long_options(parser: argparse.ArgumentParser) -> list[str]:
    opts: set[str] = set()
    for action in parser._actions:  # pylint: disable=protected-access
        for opt in action.option_strings:
            if opt.startswith("--"):
                opts.add(opt)
    return sorted(opts)


def build_bash_completion(parser: argparse.ArgumentParser, prog_name: str) -> str:
    long_opts = " ".join(_collect_long_options(parser))
    func = "_jnoveldl_tui_complete"
    base_name = Path(prog_name).name
    targets = sorted({prog_name, base_name})
    complete_lines = "\n".join(f"complete -F {func} {name}" for name in targets)
    return f"""{func}() {{
    local cur
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    COMPREPLY=( $(compgen -W '{long_opts}' -- "$cur") )
}}
{complete_lines}
"""


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browse and download J-Novel Club volumes")
    parser.add_argument(
        "--bake-credentials",
        action="store_true",
        help=(
            "Export credentials currently stored in keyring to "
            "~/.local/share/jnoveldl/credentials.json and exit"
        ),
    )
    parser.add_argument(
        "--print-completion",
        choices=["bash"],
        metavar="SHELL",
        help="Print shell completion script to stdout (currently supports: bash)",
    )
    parser.add_argument(
        "--download-all",
        action="store_true",
        help="Download all missing EPUBs from your library and exit",
    )
    return parser


def parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    return parser.parse_args()


def run_bake_credentials() -> int:
    try:
        baked_path = bake_credentials_from_keyring()
    except RuntimeError as exc:
        print(f"Failed to bake credentials: {exc}")
        return 1

    print(f"Baked credentials to {baked_path}")
    print("You can now use this tool in keyring-less console sessions.")
    return 0


# ── login (runs before curses) ───────────────────────────────────────
def ensure_login() -> tuple[JNCApi, dict]:
    keyring_ok = is_keyring_available()
    if not keyring_ok:
        print("System keyring unavailable — falling back to local credential file.")

    email, password = get_stored_credentials()
    if email and password:
        print(f"Using stored credentials for {email}")
    else:
        email, password = prompt_credentials(warn_no_keyring=not keyring_ok)
        store_credentials(email, password)

    api = JNCApi()
    try:
        api.login(email, password)
    except Exception as exc:
        print(f"\nLogin failed: {exc}")
        clear_credentials()
        sys.exit(1)

    me = api.me()
    print(f"Logged in as {me['username']}  [{me['level']}]")
    return api, me


# ── data model ───────────────────────────────────────────────────────
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


def gather_selected_books(entries: list[SeriesEntry]) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        for b in e.books:
            if b["id"] in e.selected:
                out.append(b)
    return out


# ── curses drawing helpers ───────────────────────────────────────────
def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


def _draw_bar(win, y: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h:
        return
    win.move(y, 0)
    win.clrtoeol()
    win.addnstr(y, 0, text, w - 1, attr)


# ── series list view ─────────────────────────────────────────────────
def draw_series_list(
    win, entries: list[SeriesEntry], cursor: int, scroll: int, total_sel: int
) -> None:
    win.erase()
    h, w = win.getmaxyx()
    header = " J-Novel Club Library "
    footer = " ↑↓:move  Space:toggle  Enter:volumes  d:download  c:dl+m4b  q:quit "
    status = f" {total_sel} volume(s) selected "

    # header
    _draw_bar(win, 0, header.center(w - 1), curses.A_BOLD | curses.A_REVERSE)

    # scrollable list area: rows 2 … h-3
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

    # status + footer
    _draw_bar(win, h - 2, status.center(w - 1), curses.A_BOLD)
    _draw_bar(win, h - 1, footer.center(w - 1), curses.A_DIM)
    win.refresh()


def series_list_view(win, entries: list[SeriesEntry]) -> str:
    """Interactive series list. Returns 'download', 'quit', or 'back'."""
    curses.curs_set(0)
    cursor = 0
    scroll = 0

    while True:
        h, w = win.getmaxyx()
        visible = max(1, h - 5)
        total_sel = sum(e.n_selected for e in entries)

        # keep cursor in scroll window
        if cursor < scroll:
            scroll = cursor
        if cursor >= scroll + visible:
            scroll = cursor - visible + 1

        draw_series_list(win, entries, cursor, scroll, total_sel)
        key = win.getch()

        if key == curses.KEY_UP or key == ord("k"):
            cursor = _clamp(cursor - 1, 0, len(entries) - 1)
        elif key == curses.KEY_DOWN or key == ord("j"):
            cursor = _clamp(cursor + 1, 0, len(entries) - 1)
        elif key == ord(" "):
            entries[cursor].toggle_all()
        elif key in (curses.KEY_ENTER, 10, 13):
            volume_list_view(win, entries[cursor])
        elif key == ord("d") or key == ord("D"):
            return "download"
        elif key == ord("c") or key == ord("C"):
            return "convert"
        elif key == ord("q") or key == ord("Q"):
            return "quit"


# ── volume list view (drill-down) ───────────────────────────────────
def draw_volume_list(
    win, entry: SeriesEntry, cursor: int, scroll: int
) -> None:
    win.erase()
    h, w = win.getmaxyx()
    header = f" {entry.title} "
    footer = " ↑↓:move  Space:toggle  a:all  n:none  Esc:back "
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


def volume_list_view(win, entry: SeriesEntry) -> None:
    """Drill-down into a single series' volumes."""
    cursor = 0
    scroll = 0

    while True:
        h, w = win.getmaxyx()
        visible = max(1, h - 5)

        if cursor < scroll:
            scroll = cursor
        if cursor >= scroll + visible:
            scroll = cursor - visible + 1

        draw_volume_list(win, entry, cursor, scroll)
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
        elif key == 27:  # Esc
            return


# ── download phase (outside curses) ─────────────────────────────────
def run_downloads(books: list[dict]) -> None:
    """Download selected books with a delay between each."""
    capped = books
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    skipped = 0
    for i, bk in enumerate(capped, 1):
        link = bk.get("epub_link")
        slug = bk.get("slug") or bk["id"]
        dest = DOWNLOAD_DIR / f"{slug}.epub"

        print(f"  [{i}/{len(capped)}] {bk['title']} … ", end="", flush=True)

        if dest.exists():
            print("skip (already exists)")
            skipped += 1
            continue

        if not link:
            print("✗ no EPUB link")
            continue

        try:
            nbytes = download_epub(link, str(dest))
            mb = nbytes / (1024 * 1024)
            print(f"{mb:.1f} MB ✓")
            ok += 1
        except Exception as exc:
            print(f"FAILED ({exc})")

        if i < len(capped):
            time.sleep(DOWNLOAD_DELAY)

    print(f"\n  Done — {ok} downloaded, {skipped} skipped (already on disk).")


def collect_downloadable_books(grouped: dict[str, dict]) -> list[dict]:
    books: list[dict] = []
    for info in grouped.values():
        for bk in info["books"]:
            if bk.get("epub_link"):
                books.append(bk)
    return books


# ── conversion phase (outside curses) ────────────────────────────────
def run_conversions(books: list[dict]) -> None:
    """Convert downloaded EPUBs to M4B audiobooks, skipping existing."""
    AUDIOBOOK_DIR.mkdir(parents=True, exist_ok=True)

    # Filter to the books that actually need conversion
    to_convert: list[tuple[int, dict, Path, Path]] = []
    skipped = 0
    for i, bk in enumerate(books, 1):
        slug = bk.get("slug") or bk["id"]
        epub_path = DOWNLOAD_DIR / f"{slug}.epub"
        m4b_path = AUDIOBOOK_DIR / f"{slug}.m4b"

        if m4b_path.exists():
            print(f"  [{i}/{len(books)}] {bk['title']}")
            print("    → skip (m4b already exists)")
            skipped += 1
        elif not epub_path.exists():
            print(f"  [{i}/{len(books)}] {bk['title']}")
            print("    → skip (epub not found)")
        else:
            to_convert.append((i, bk, epub_path, m4b_path))

    if not to_convert:
        print(f"\n  Done — 0 converted, {skipped} skipped, 0 failed.")
        return

    ok = 0
    failures: list[tuple[str, str]] = []  # (title, error message)
    for i, bk, epub_path, m4b_path in to_convert:
        print(f"  [{i}/{len(books)}] {bk['title']}")
        try:
            result = convert_epub_to_m4b(epub_path, m4b_path)
            mb = result.stat().st_size / (1024 * 1024)
            print(f"    → {mb:.1f} MB ✓")
            ok += 1
        except Exception as exc:
            print(f"    → FAILED ({exc})")
            failures.append((bk["title"], str(exc)))

    print(f"\n  Done — {ok} converted, {skipped} skipped, {len(failures)} failed.")
    if failures:
        print("\n  Failed conversions:")
        for title, err in failures:
            print(f"    • {title}: {err}")


# ── main ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = create_parser()
    args = parse_args(parser)

    if args.print_completion == "bash":
        print(build_bash_completion(parser, sys.argv[0]))
        return

    if args.bake_credentials:
        sys.exit(run_bake_credentials())

    # Phase 1: login + fetch (normal terminal)
    api, me = ensure_login()
    print("Fetching library … ", end="", flush=True)
    lib = api.library(limit=500)
    books_raw = lib.get("books", [])
    grouped = parse_library(books_raw)
    total = sum(len(s["books"]) for s in grouped.values())
    print(f"{total} books across {len(grouped)} series.\n")

    if args.download_all:
        downloadable = collect_downloadable_books(grouped)
        if not downloadable:
            print("No downloadable EPUBs found in library.")
            return

        print(f"Downloading all missing EPUBs ({len(downloadable)} total with links) …\n")
        run_downloads(downloadable)
        return

    entries = build_entries(grouped)

    # Phase 2: curses TUI
    result = curses.wrapper(lambda win: series_list_view(win, entries))

    # Phase 3: download (normal terminal again)
    if result in ("download", "convert"):
        selected = gather_selected_books(entries)
        if not selected:
            print("Nothing selected.")
        else:
            print(f"\nDownloading {len(selected)} volume(s) …\n")
            run_downloads(selected)

            if result == "convert":
                print(f"\nConverting {len(selected)} volume(s) to M4B …\n")
                run_conversions(selected)
    else:
        print("Bye!")


if __name__ == "__main__":
    main()
