"""Abstract UI base + action enum.

A concrete UI subclass implements `run(entries) -> Action` to drive the
interactive flow. Progress hooks (`on_phase_start`, `on_item_start`,
`on_item_result`, `on_phase_done`, `on_info`, `on_error`) are called by
`LibraryController` during downloads/conversions; defaults print to
stdout/stderr so the standard terminal output is preserved.
"""

from __future__ import annotations

import enum
import sys
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import SeriesEntry


class Action(enum.Enum):
    QUIT = "quit"
    DOWNLOAD = "download"
    CONVERT = "convert"
    EXTEND = "extend"   # single-book: append new EPUB chapters onto existing m4b
    BAKE = "bake"       # single-book: re-encode existing m4b to Opus MKA (48 kHz)


class LibraryUI(ABC):
    """Abstract frontend for the jnoveldl library browser."""

    # Set by the UI when returning Action.EXTEND or Action.BAKE. The book dict
    # matches the shape used everywhere else (id, slug, title, optional path, ...).
    pending_action_book: dict | None = None

    @abstractmethod
    def run(self, entries: "list[SeriesEntry]") -> Action:
        """Run the interactive UI and return the user's chosen action."""

    # ── progress hooks (default: print to terminal) ────────────────
    def on_phase_start(self, phase: str, total: int) -> None:
        if phase == "download":
            print(f"\nDownloading {total} volume(s) …\n")
        elif phase == "convert":
            print(f"\nConverting {total} volume(s) to M4B …\n")
        elif phase == "extend":
            print(f"\nExtending {total} audiobook(s) with new EPUB chapters …\n")
        elif phase == "bake":
            print(f"\nBaking {total} audiobook(s) to Opus MKA (48 kHz) …\n")

    def on_item_start(self, phase: str, index: int, total: int, title: str) -> None:
        if phase == "download":
            print(f"  [{index}/{total}] {title} … ", end="", flush=True)
        else:
            print(f"  [{index}/{total}] {title}")

    def on_item_result(self, phase: str, status: str, detail: str = "") -> None:
        """status ∈ {ok, skip, fail, no-link, no-epub, no-m4b, nothing-new}."""
        if phase == "download":
            if status == "ok":
                print(detail or "✓")
            elif status == "skip":
                print("skip (already exists)")
            elif status == "no-link":
                print("✗ no EPUB link")
            elif status == "fail":
                print(f"FAILED ({detail})")
            else:
                print(status)
        elif phase == "extend":
            if status == "ok":
                print(f"    → {detail} ✓" if detail else "    → ✓")
            elif status == "nothing-new":
                print("    → already up to date (no new chapters)")
            elif status == "no-epub":
                print("    → skip (epub not found)")
            elif status == "no-m4b":
                print("    → skip (no existing m4b — run convert first)")
            elif status == "fail":
                print(f"    → FAILED ({detail})")
            else:
                print(f"    → {status}")
        elif phase == "bake":
            if status == "ok":
                print(f"    → {detail} ✓" if detail else "    → ✓")
            elif status == "skip":
                print("    → skip (mka already exists)")
            elif status == "no-m4b":
                print("    → skip (no existing m4b — run convert first)")
            elif status == "fail":
                print(f"    → FAILED ({detail})")
            else:
                print(f"    → {status}")
        else:  # convert
            if status == "ok":
                print(f"    → {detail} ✓" if detail else "    → ✓")
            elif status == "skip":
                print("    → skip (m4b already exists)")
            elif status == "no-epub":
                print("    → skip (epub not found)")
            elif status == "fail":
                print(f"    → FAILED ({detail})")
            else:
                print(f"    → {status}")

    def on_phase_done(self, phase: str, summary: dict) -> None:
        if phase == "download":
            ok = summary.get("ok", 0)
            skipped = summary.get("skipped", 0)
            print(f"\n  Done — {ok} downloaded, {skipped} skipped (already on disk).")
        elif phase == "convert":
            ok = summary.get("ok", 0)
            skipped = summary.get("skipped", 0)
            failures = summary.get("failures", [])
            print(f"\n  Done — {ok} converted, {skipped} skipped, {len(failures)} failed.")
            if failures:
                print("\n  Failed conversions:")
                for title, err in failures:
                    print(f"    • {title}: {err}")
        elif phase == "extend":
            ok = summary.get("ok", 0)
            skipped = summary.get("skipped", 0)
            failures = summary.get("failures", [])
            print(f"\n  Done — {ok} extended, {skipped} unchanged, {len(failures)} failed.")
            if failures:
                print("\n  Failed extensions:")
                for title, err in failures:
                    print(f"    • {title}: {err}")
        elif phase == "bake":
            ok = summary.get("ok", 0)
            skipped = summary.get("skipped", 0)
            failures = summary.get("failures", [])
            print(f"\n  Done — {ok} baked, {skipped} skipped, {len(failures)} failed.")
            if failures:
                print("\n  Failed bakes:")
                for title, err in failures:
                    print(f"    • {title}: {err}")

    def on_info(self, message: str) -> None:
        print(message)

    def on_error(self, message: str) -> None:
        print(message, file=sys.stderr)
