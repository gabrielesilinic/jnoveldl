#!/usr/bin/env python3
"""jnoveldl — TUI for browsing and downloading your J-Novel Club library.

Two UI backends are available:

  * curses (default on Linux/macOS) — full-screen, arrow-key driven.
  * plain  (default on Windows, also via --no-curses) — numbered-menu REPL,
    stdlib only, works in dumb terminals.

Use --backend {curses,plain,auto} or --no-curses to choose explicitly.

Curses bindings — series list:
  ↑/↓       navigate            Space  toggle all volumes in series
  Enter     drill into volumes  d      download selected
  c         download + convert to m4b  q  quit
Curses bindings — volume list:
  ↑/↓       navigate            Space  toggle volume
  a         select all          n      deselect all
  e         extend m4b          m      bake m4b → Opus MKA (48 kHz)
  Esc       back to series

Plain backend: type '?' at any prompt for in-app help.
"""

import argparse
import sys
from pathlib import Path

from jnoveldl import (
    JNCApi,
    bake_credentials_from_keyring,
    clear_credentials,
    get_stored_credentials,
    is_keyring_available,
    parse_library,
    prompt_credentials,
    store_credentials,
)
from ui import (
    Action,
    LibraryController,
    build_entries,
    build_extras_entry,
    collect_downloadable_books,
    gather_selected_books,
    get_ui,
)
from ui.plain_ui import PlainUI

DOWNLOAD_DIR = Path("downloads")
EXTRAS_DIR = DOWNLOAD_DIR / "extras"
AUDIOBOOK_DIR = Path("audiobooks")


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
    parser.add_argument(
        "--backend",
        choices=["curses", "plain", "auto"],
        default="auto",
        help="UI backend (default: auto — curses on Linux/macOS, plain on Windows)",
    )
    parser.add_argument(
        "--no-curses",
        action="store_true",
        help="Alias for --backend plain (forces the plain-text REPL UI)",
    )
    return parser


def parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    return parser.parse_args()


def resolve_backend(args: argparse.Namespace) -> str:
    """Resolve CLI flags to a backend name ('curses', 'plain', or 'auto')."""
    if args.no_curses and args.backend not in (None, "auto", "plain"):
        print(
            f"Warning: --no-curses overridden by --backend {args.backend}",
            file=sys.stderr,
        )
    if args.no_curses and args.backend in (None, "auto"):
        return "plain"
    return args.backend or "auto"


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


def _instantiate_ui(name: str):
    """Instantiate the chosen UI, falling back to PlainUI on curses failure."""
    try:
        ui_cls = get_ui(name)
        return ui_cls()
    except Exception as exc:  # pylint: disable=broad-except
        if name == "plain":
            raise
        print(
            f"Failed to initialise '{name}' UI ({exc}); falling back to plain.",
            file=sys.stderr,
        )
        return PlainUI()


# ── main ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = create_parser()
    args = parse_args(parser)

    if args.print_completion == "bash":
        print(build_bash_completion(parser, sys.argv[0]))
        return

    if args.bake_credentials:
        sys.exit(run_bake_credentials())

    # Phase 1: login + fetch
    api, _me = ensure_login()
    print("Fetching library … ", end="", flush=True)
    lib = api.library(limit=500)
    books_raw = lib.get("books", [])
    grouped = parse_library(books_raw)
    total = sum(len(s["books"]) for s in grouped.values())
    print(f"{total} books across {len(grouped)} series.\n")

    controller = LibraryController(DOWNLOAD_DIR, AUDIOBOOK_DIR)

    if args.download_all:
        downloadable = collect_downloadable_books(grouped)
        if not downloadable:
            print("No downloadable EPUBs found in library.")
            return

        print(
            f"Downloading all missing EPUBs ({len(downloadable)} total with links) …"
        )
        controller.run_downloads(downloadable, PlainUI())
        return

    entries = build_entries(grouped)
    extras_entry = build_extras_entry(EXTRAS_DIR)
    if extras_entry:
        entries.append(extras_entry)
        print(f"Found {len(extras_entry.books)} local extra(s) in {EXTRAS_DIR}/.")

    # Phase 2: interactive UI
    backend = resolve_backend(args)
    ui = _instantiate_ui(backend)
    try:
        action = ui.run(entries)
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(ui, PlainUI):
            raise
        print(
            f"\nUI '{backend}' crashed ({exc}); retrying with plain backend.",
            file=sys.stderr,
        )
        ui = PlainUI()
        action = ui.run(entries)

    # Phase 3: dispatch
    if action == Action.EXTEND:
        book = ui.pending_action_book
        if book is None:
            ui.on_error("EXTEND requested but no book was selected.")
            return
        controller.run_single_extend(book, ui)
    elif action == Action.BAKE:
        book = ui.pending_action_book
        if book is None:
            ui.on_error("BAKE requested but no book was selected.")
            return
        controller.run_single_bake(book, ui)
    elif action in (Action.DOWNLOAD, Action.CONVERT):
        selected = gather_selected_books(entries)
        if not selected:
            ui.on_info("Nothing selected.")
            return
        controller.run_downloads(selected, ui)
        if action == Action.CONVERT:
            controller.run_conversions(selected, ui)
    else:
        ui.on_info("Bye!")


if __name__ == "__main__":
    main()
