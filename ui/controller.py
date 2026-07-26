"""Library controller: orchestrates downloads & conversions, reporting
progress through `LibraryUI` hooks instead of bare prints.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from bookconvert import bake_m4b_to_mka, convert_epub_to_m4b
from bookextend import extend_m4b_with_new_chapters
from jnoveldl import download_epub

if TYPE_CHECKING:
    from .base import LibraryUI

DOWNLOAD_DELAY = 2  # seconds between consecutive downloads


class LibraryController:
    def __init__(
        self,
        download_dir: Path,
        audiobook_dir: Path,
        download_delay: float = DOWNLOAD_DELAY,
    ):
        self.download_dir = download_dir
        self.audiobook_dir = audiobook_dir
        self.download_delay = download_delay

    # ── download ──────────────────────────────────────────────────
    def run_downloads(self, books: list[dict], ui: "LibraryUI") -> dict:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        total = len(books)
        ui.on_phase_start("download", total)

        ok = 0
        skipped = 0
        for i, bk in enumerate(books, 1):
            link = bk.get("epub_link")
            slug = bk.get("slug") or bk["id"]
            dest = self.download_dir / f"{slug}.epub"

            ui.on_item_start("download", i, total, bk["title"])

            if bk.get("extra"):
                # Local extras are never downloaded — silently treated as done.
                ui.on_item_result("download", "skip")
                skipped += 1
                continue

            if dest.exists():
                ui.on_item_result("download", "skip")
                skipped += 1
                continue

            if not link:
                ui.on_item_result("download", "no-link")
                continue

            try:
                nbytes = download_epub(link, str(dest))
                mb = nbytes / (1024 * 1024)
                ui.on_item_result("download", "ok", f"{mb:.1f} MB ✓")
                ok += 1
            except Exception as exc:  # pylint: disable=broad-except
                ui.on_item_result("download", "fail", str(exc))

            if i < total:
                time.sleep(self.download_delay)

        summary = {"ok": ok, "skipped": skipped, "total": total}
        ui.on_phase_done("download", summary)
        return summary

    # ── convert ───────────────────────────────────────────────────
    def run_conversions(self, books: list[dict], ui: "LibraryUI") -> dict:
        self.audiobook_dir.mkdir(parents=True, exist_ok=True)
        total = len(books)
        ui.on_phase_start("convert", total)

        to_convert: list[tuple[int, dict, Path, Path]] = []
        skipped = 0
        for i, bk in enumerate(books, 1):
            slug = bk.get("slug") or bk["id"]
            if bk.get("path"):
                epub_path = Path(bk["path"])
            else:
                epub_path = self.download_dir / f"{slug}.epub"
            m4b_path = self.audiobook_dir / f"{slug}.m4b"

            if m4b_path.exists():
                ui.on_item_start("convert", i, total, bk["title"])
                ui.on_item_result("convert", "skip")
                skipped += 1
            elif not epub_path.exists():
                ui.on_item_start("convert", i, total, bk["title"])
                ui.on_item_result("convert", "no-epub")
            else:
                to_convert.append((i, bk, epub_path, m4b_path))

        ok = 0
        failures: list[tuple[str, str]] = []
        for i, bk, epub_path, m4b_path in to_convert:
            ui.on_item_start("convert", i, total, bk["title"])
            try:
                result = convert_epub_to_m4b(epub_path, m4b_path)
                mb = result.stat().st_size / (1024 * 1024)
                ui.on_item_result("convert", "ok", f"{mb:.1f} MB")
                ok += 1
            except Exception as exc:  # pylint: disable=broad-except
                ui.on_item_result("convert", "fail", str(exc))
                failures.append((bk["title"], str(exc)))

        summary = {
            "ok": ok,
            "skipped": skipped,
            "failures": failures,
            "total": total,
        }
        ui.on_phase_done("convert", summary)
        return summary

    # ── extend (single book) ─────────────────────────────────────
    def run_single_extend(self, book: dict, ui: "LibraryUI") -> dict:
        """Append new EPUB chapters onto an existing M4B for ONE book.

        Intended for growing extras (e.g. web novels). Existing audio is
        preserved via lossless AAC concat — only the new chapters are
        synthesised.
        """
        self.audiobook_dir.mkdir(parents=True, exist_ok=True)
        ui.on_phase_start("extend", 1)

        slug = book.get("slug") or book["id"]
        if book.get("path"):
            epub_path = Path(book["path"])
        else:
            epub_path = self.download_dir / f"{slug}.epub"
        m4b_path = self.audiobook_dir / f"{slug}.m4b"

        ui.on_item_start("extend", 1, 1, book["title"])

        ok = 0
        skipped = 0
        failures: list[tuple[str, str]] = []
        if not epub_path.exists():
            ui.on_item_result("extend", "no-epub")
        elif not m4b_path.exists():
            ui.on_item_result("extend", "no-m4b")
        else:
            try:
                before = m4b_path.stat().st_mtime
                result = extend_m4b_with_new_chapters(epub_path, m4b_path)
                after = result.stat().st_mtime
                if after <= before:
                    ui.on_item_result("extend", "nothing-new")
                else:
                    mb = result.stat().st_size / (1024 * 1024)
                    ui.on_item_result("extend", "ok", f"{mb:.1f} MB")
                    ok = 1
            except Exception as exc:  # pylint: disable=broad-except
                ui.on_item_result("extend", "fail", str(exc))
                failures.append((book["title"], str(exc)))

        summary = {
            "ok": ok,
            "skipped": skipped,
            "failures": failures,
            "total": 1,
        }
        ui.on_phase_done("extend", summary)
        return summary

    # ── bake (single book) ───────────────────────────────────────
    def run_single_bake(self, book: dict, ui: "LibraryUI") -> dict:
        """Re-encode ONE book's existing M4B into an Opus MKA at 48 kHz.

        Optional size-saving measure for very big files (e.g. long-running
        extras). If the M4B does not exist yet, run the normal EPUB download
        and conversion pipeline first. The source m4b is left untouched.
        """
        slug = book.get("slug") or book["id"]
        m4b_path = self.audiobook_dir / f"{slug}.m4b"
        mka_path = self.audiobook_dir / f"{slug}.mka"

        if not m4b_path.exists():
            self.run_downloads([book], ui)
            self.run_conversions([book], ui)

        ui.on_phase_start("bake", 1)
        ui.on_item_start("bake", 1, 1, book["title"])

        ok = 0
        skipped = 0
        failures: list[tuple[str, str]] = []
        if not m4b_path.exists():
            ui.on_item_result("bake", "no-m4b")
        else:
            try:
                result = bake_m4b_to_mka(m4b_path, mka_path)
                mb = result.stat().st_size / (1024 * 1024)
                ui.on_item_result("bake", "ok", f"{mb:.1f} MB")
                ok = 1
            except Exception as exc:  # pylint: disable=broad-except
                ui.on_item_result("bake", "fail", str(exc))
                failures.append((book["title"], str(exc)))

        summary = {
            "ok": ok,
            "skipped": skipped,
            "failures": failures,
            "total": 1,
        }
        ui.on_phase_done("bake", summary)
        return summary
