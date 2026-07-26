"""
Extend an existing M4B audiobook with chapters newly appended to its source
EPUB. Intended for web-novel ebooks (e.g. The Archmage Coefficient) where the
purchased EPUB grows over time and a full re-synthesis would be wasteful.

Kept deliberately separate from bookconvert.py so the core synth pipeline
isn't muddied by extension/append concerns. We do reuse a few primitives
from bookconvert (TTS engine instance, ffmetadata writer, encoding params)
to avoid loading the model twice and to guarantee the new segment is
byte-compatible with the existing AAC stream — enabling a lossless
`-c copy` concat instead of a full re-encode of the existing audio.

Assumptions (per the user's promise about Archmage-style growing EPUBs):
  * Chapter ordering is stable; new chapters are appended to the tail.
  * The first N chapters of the new EPUB match the N chapters already in
    the M4B (title text may drift slightly; we warn but don't abort).
  * The existing M4B was produced by this same pipeline (AAC LC, 24 kHz,
    mono, ~64 k bitrate) — required for lossless concat-copy.
"""

from __future__ import annotations

import gc
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from ebooklib import epub
from tqdm import tqdm

from bookconvert import (
    AAC_BITRATE,
    AUDIOBOOK_DIR,
    PLACEHOLDER_AUTHOR,
    SR,
    _tts_engine,
    _write_ffmetadata,
)
from epubparser import extract_chapters, get_cover
from tts import TTSInferenceEngine


# ══════════════════════════════════════════════════════════════
# ffprobe helpers
# ══════════════════════════════════════════════════════════════

def _probe_m4b(m4b_path: Path) -> dict:
    """Return {'duration_s', 'chapters': [{'title','start_ms','end_ms'}]}."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_chapters",
        str(m4b_path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    duration_s = float(data["format"]["duration"])
    chapters = []
    for ch in data.get("chapters", []):
        title = (ch.get("tags") or {}).get("title", "") or ""
        # Prefer integer start/end if the time_base is 1/1000 (our writer's choice).
        tb = ch.get("time_base", "")
        if tb == "1/1000" and "start" in ch and "end" in ch:
            start_ms = int(ch["start"])
            end_ms = int(ch["end"])
        else:
            start_ms = int(round(float(ch["start_time"]) * 1000))
            end_ms = int(round(float(ch["end_time"]) * 1000))
        chapters.append({"title": title, "start_ms": start_ms, "end_ms": end_ms})
    return {"duration_s": duration_s, "chapters": chapters}


# ══════════════════════════════════════════════════════════════
# Chapter-marker simulation
# ══════════════════════════════════════════════════════════════

def _simulate_markers(work: list[tuple[int, str, str]]) -> tuple[list[int], list[str]]:
    """Replay bookconvert's untitled-merge logic.

    Returns (indices, marker_titles) where indices[i] is the 0-based final
    chapter-marker index that work[i] contributes to.
    """
    marker_titles: list[str] = []
    indices: list[int] = []
    for _ci, title, _text in work:
        ch_title = (title or "").strip()
        if ch_title == "" and marker_titles:
            indices.append(len(marker_titles) - 1)
        else:
            marker_titles.append(ch_title or f"Chapter {len(marker_titles) + 1}")
            indices.append(len(marker_titles) - 1)
    return indices, marker_titles


# ══════════════════════════════════════════════════════════════
# Atomic-ish replace with backup
# ══════════════════════════════════════════════════════════════

def _hardlink_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ══════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════

def extend_m4b_with_new_chapters(
    epub_path: str | Path,
    m4b_path: str | Path | None = None,
    voice: str = "af_heart",
    speed: float = 1.0,
    language: str | None = None,
    engine: Optional[TTSInferenceEngine] = None,
    strict_titles: bool = False,
) -> Path:
    """Append newly-added EPUB chapters onto an existing M4B in place.

    Returns the path of the (now-extended) M4B. If nothing new is found,
    the original is returned untouched.
    """
    epub_path = Path(epub_path)
    if not epub_path.exists():
        raise FileNotFoundError(f"EPUB not found: {epub_path}")

    if m4b_path is None:
        AUDIOBOOK_DIR.mkdir(parents=True, exist_ok=True)
        m4b_path = AUDIOBOOK_DIR / f"{epub_path.stem}.m4b"
    m4b_path = Path(m4b_path)
    if not m4b_path.exists():
        raise FileNotFoundError(
            f"No existing M4B to extend at {m4b_path}. "
            "Use the standard convert flow for first synthesis."
        )

    # ── Probe existing audiobook ──────────────────────────────
    print(f"  Probing existing audiobook: {m4b_path.name}")
    probe = _probe_m4b(m4b_path)
    existing_chapters = probe["chapters"]
    existing_duration_s = probe["duration_s"]
    K = len(existing_chapters)
    print(f"  Existing: {K} chapter(s), {existing_duration_s / 60:.1f} min")
    if K == 0:
        raise RuntimeError(
            "Existing M4B has no chapter markers — refusing to extend "
            "(can't align new content)."
        )

    # ── Parse new EPUB ────────────────────────────────────────
    book = epub.read_epub(str(epub_path))
    book_title = next(
        (t[0] for t in (book.get_metadata("DC", "title") or [])),
        epub_path.stem,
    )
    try:
        author = book.get_metadata("DC", "creator")[0][0]
    except Exception:  # pylint: disable=broad-except
        author = PLACEHOLDER_AUTHOR
    if not author:
        author = PLACEHOLDER_AUTHOR

    chapters = extract_chapters(book)
    coverdata = get_cover(book)
    del book
    gc.collect()

    work = [
        (ci, ch["title"], ch["text"].strip())
        for ci, ch in enumerate(chapters)
        if ch["text"].strip()
    ]
    del chapters
    if not work:
        raise RuntimeError(f"No text chapters found in {epub_path.name}")

    indices, marker_titles = _simulate_markers(work)
    sim_total = len(marker_titles)
    print(f"  New EPUB → {sim_total} chapter marker(s)")

    if sim_total < K:
        raise RuntimeError(
            f"New EPUB yields fewer markers ({sim_total}) than the existing "
            f"M4B has ({K}). Refusing to extend — looks like the wrong file."
        )
    if sim_total == K:
        print("  Nothing to extend: marker counts match.")
        return m4b_path

    # ── Sanity-check first K titles ───────────────────────────
    mismatches = []
    for i in range(K):
        a = existing_chapters[i]["title"].strip()
        b = marker_titles[i].strip()
        if a != b:
            mismatches.append((i, a, b))
    if mismatches:
        print(f"  ⚠️  {len(mismatches)} chapter title mismatch(es) "
              f"between existing M4B and new EPUB:")
        for i, a, b in mismatches[:5]:
            print(f"      [{i}]  m4b: {a!r}")
            print(f"            epub: {b!r}")
        if len(mismatches) > 5:
            print(f"      … and {len(mismatches) - 5} more")
        if strict_titles:
            raise RuntimeError(
                "Aborting: chapter titles diverged (strict_titles=True)."
            )
        print("  Continuing — chapter count still aligns.")

    # ── Locate first work entry belonging to a new marker ─────
    start_idx = next((i for i, mi in enumerate(indices) if mi >= K), None)
    assert start_idx is not None  # sim_total > K guarantees this
    new_work = work[start_idx:]
    new_markers = marker_titles[K:]
    n_new_markers = len(new_markers)
    print(f"  Will synth {len(new_work)} entry/entries → {n_new_markers} new marker(s)")

    total_words = sum(len(t.split()) for _, _, t in new_work)

    # ── Workspace ─────────────────────────────────────────────
    tmpd = m4b_path.parent / f"{m4b_path.stem}.extend.tmp"
    if tmpd.exists():
        shutil.rmtree(tmpd)
    tmpd.mkdir(parents=True, exist_ok=True)

    active_engine = engine or _tts_engine
    active_engine.start()

    # ── Synthesise new content → tmpd/new.wav ─────────────────
    wav_path = tmpd / "new.wav"
    gap = np.zeros(int(round(SR * 0.2)), dtype=np.int16)
    gap_len = gap.shape[0]

    bar = tqdm(
        total=total_words,
        desc=f"  {book_title[:50]} (+extend)",
        unit="words",
        bar_format="  {desc} |{bar}| {percentage:3.0f}% {n_fmt}/{total_fmt} words [{elapsed}<{remaining}]",
        dynamic_ncols=True,
    )

    # The new segment opens with a 0.2 s gap so the seam between the
    # existing tail and the first new chapter doesn't sound jammed together.
    new_chapters_ff: list[dict] = []
    cursor = 0
    first_chunk = True

    with sf.SoundFile(str(wav_path), mode="w", samplerate=SR,
                      channels=1, subtype="PCM_16") as wf:
        wf.write(gap)            # lead-in gap separating from existing audio
        cursor += gap_len

        for pos, (_ci, title, text) in enumerate(new_work, 1):
            ch_words = len(text.split())
            label = title if len(title) <= 40 else title[:37] + "…"
            bar.set_postfix_str(f"… {pos}/{len(new_work)} {label}")
            ch_start = cursor

            for chunk in active_engine.generate_chunks(
                text=text, voice=voice, speed=speed, language=language,
            ):
                if not first_chunk:
                    wf.write(gap)
                    cursor += gap_len
                wf.write(chunk)
                cursor += chunk.shape[0]
                first_chunk = False
                del chunk
            del text
            ch_end = cursor

            ch_title = (title or "").strip()
            if ch_title == "" and new_chapters_ff:
                new_chapters_ff[-1]["end_ms_local"] = int((ch_end * 1000) // SR)
            else:
                new_chapters_ff.append({
                    "title": ch_title or f"Chapter {K + len(new_chapters_ff) + 1}",
                    "start_ms_local": int((ch_start * 1000) // SR),
                    "end_ms_local": int((ch_end * 1000) // SR),
                })
            bar.update(ch_words)

    bar.close()
    new_audio_s = cursor / SR
    print(f"  New audio: {new_audio_s / 60:.1f} min, {len(new_chapters_ff)} marker(s)")
    assert len(new_chapters_ff) == n_new_markers, (
        f"marker count drift: simulated {n_new_markers}, produced {len(new_chapters_ff)}"
    )

    # ── Encode new WAV → AAC m4a matching existing stream ─────
    new_m4a = tmpd / "new.m4a"
    print("  Encoding new segment to AAC …", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(wav_path),
        "-ar", "24000", "-ac", "1",
        "-c:a", "aac", "-b:a", AAC_BITRATE,
        str(new_m4a),
    ], check=True)
    wav_path.unlink(missing_ok=True)

    # ── Strip existing m4b to audio-only m4a (no chapters/cover) ─
    existing_m4a = tmpd / "existing.m4a"
    print("  Demuxing existing audio stream …", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(m4b_path),
        "-map", "0:a:0",
        "-c:a", "copy",
        str(existing_m4a),
    ], check=True)

    # ── Lossless concat: existing.m4a + new.m4a → concat.m4a ──
    concat_list = tmpd / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        f.write(f"file '{existing_m4a.resolve()}'\n")
        f.write(f"file '{new_m4a.resolve()}'\n")
    concat_m4a = tmpd / "concat.m4a"
    print("  Concatenating (lossless copy) …", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(concat_m4a),
    ], check=True)

    # ── Build merged ffmetadata ───────────────────────────────
    offset_ms = int(round(existing_duration_s * 1000))
    all_chapters_ff: list[dict] = []
    for ch in existing_chapters:
        all_chapters_ff.append({
            "title": ch["title"],
            "start_ms": ch["start_ms"],
            "end_ms": ch["end_ms"],
        })
    for ch in new_chapters_ff:
        all_chapters_ff.append({
            "title": ch["title"],
            "start_ms": offset_ms + ch["start_ms_local"],
            "end_ms": offset_ms + ch["end_ms_local"],
        })
    meta_path = tmpd / "chapters.ffmeta"
    _write_ffmetadata(str(meta_path), book_title, author, all_chapters_ff)

    # ── Cover: prefer new EPUB's cover ───────────────────────
    cover_input_args: list = []
    cover_output_map: list = []
    if coverdata:
        ext = (mimetypes.guess_extension(coverdata.get("mime", "")) or "").lower()
        if ext in (".jpe", ".jpeg"):
            ext = ".jpg"
        if ext == "":
            ext = ".jpg"
        cover_path = tmpd / f"cover{ext}"
        with open(cover_path, "wb") as f:
            f.write(coverdata["bytes"])
        del coverdata
        cover_input_args = ["-i", str(cover_path)]
        vcodec = "png" if ext == ".png" else "mjpeg"
        cover_output_map = [
            "-map", "2:v:0", "-c:v:0", vcodec,
            "-disposition:v:0", "attached_pic",
        ]

    # ── Final mux ─────────────────────────────────────────────
    out_path = m4b_path.with_suffix(".m4b.new")
    print("  Muxing final M4B …", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(concat_m4a),
        "-f", "ffmetadata", "-i", str(meta_path),
        *cover_input_args,
        "-map", "0:a",
        *cover_output_map,
        "-map_metadata", "1", "-map_chapters", "1",
        "-c:a", "copy",
        "-f", "ipod",
        str(out_path),
    ], check=True)

    # ── Swap into place, keep backup on disk briefly ─────────
    backup = m4b_path.with_suffix(".m4b.bak")
    if backup.exists():
        backup.unlink()
    _hardlink_or_copy(m4b_path, backup)
    os.replace(out_path, m4b_path)
    backup.unlink(missing_ok=True)

    shutil.rmtree(tmpd, ignore_errors=True)

    size_mb = m4b_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ {m4b_path.name} extended → "
          f"{size_mb:.1f} MB, +{n_new_markers} chapter(s)")
    return m4b_path
