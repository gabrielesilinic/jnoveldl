"""
EPUB → M4B audiobook converter (single-process, tqdm progress).

Loads the TTS model once and synthesises chapters sequentially,
streaming audio to disk one sentence at a time to minimise memory use.
"""

import gc
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from ebooklib import epub
from tqdm import tqdm

from epubparser import extract_chapters, get_cover
from tts import TTSInferenceEngine
from tts.kokoro_tts_inference_engine import KokoroTTSInferenceEngine as ActiveTTSInferenceEngine

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

SR = 24000
AAC_BITRATE = "64k"
PLACEHOLDER_AUTHOR = "TTS Placeholder"
AUDIOBOOK_DIR = Path("audiobooks")

# ══════════════════════════════════════════════════════════════
# Audio helpers
# ══════════════════════════════════════════════════════════════

def _write_ffmetadata(path: str, title: str | None, author: str, chapters):
    lines = [";FFMETADATA1"]
    if title:
        lines += [f"title={title}", f"album={title}"]
    if author:
        lines += [f"artist={author}", f"album_artist={author}"]
    for ch in chapters:
        ch_title = (ch["title"] or "").replace("\n", " ").strip()
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={ch['start_ms']}")
        lines.append(f"END={max(ch['end_ms'], ch['start_ms'] + 1)}")
        lines.append(f"title={ch_title}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


_tts_engine = ActiveTTSInferenceEngine()


# ══════════════════════════════════════════════════════════════
# High-level convert function (called from TUI)
# ══════════════════════════════════════════════════════════════

def convert_epub_to_m4b(
    epub_path: str | Path,
    output_path: str | Path | None = None,
    voice: str = "af_heart",
    speed: float = 1.0,
    language: str | None = None,
    engine: TTSInferenceEngine | None = None,
) -> Path:
    """Convert a single EPUB file to M4B audiobook with tqdm progress.

    The TTS model is loaded lazily on first call and reused across
    subsequent calls automatically.

    Returns the Path to the finished .m4b file.
    """
    epub_path = Path(epub_path)
    if not epub_path.exists():
        raise FileNotFoundError(f"EPUB not found: {epub_path}")

    # ── Parse EPUB, extract metadata, then release it ─────────
    book = epub.read_epub(str(epub_path))
    book_title = next(
        (t[0] for t in (book.get_metadata("DC", "title") or [])),
        epub_path.stem,
    )
    try:
        author = book.get_metadata("DC", "creator")[0][0]
    except Exception:
        author = PLACEHOLDER_AUTHOR
    if not author:
        author = PLACEHOLDER_AUTHOR

    chapters = extract_chapters(book)
    coverdata = get_cover(book)
    del book
    gc.collect()

    # ── Build work list (chapter index + text) ────────────────
    work = [
        (ci, ch["title"], ch["text"].strip())
        for ci, ch in enumerate(chapters)
        if ch["text"].strip()
    ]
    if not work:
        raise RuntimeError(f"No text chapters found in {epub_path.name}")

    total_words = sum(len(text.split()) for _, _, text in work)
    del chapters

    # ── Output / temp paths ───────────────────────────────────
    if output_path is None:
        AUDIOBOOK_DIR.mkdir(parents=True, exist_ok=True)
        output_path = AUDIOBOOK_DIR / f"{epub_path.stem}.m4b"
    output_path = Path(output_path)

    tmpd = output_path.parent / f"{output_path.stem}.tmp"
    tmpd.mkdir(parents=True, exist_ok=True)

    active_engine = engine or _tts_engine
    active_engine.start()

    # ── Run conversion ────────────────────────────────────────
    _run_conversion(
        engine=active_engine,
        work=work,
        total_words=total_words,
        book_title=book_title,
        author=author,
        coverdata=coverdata,
        tmpd=tmpd,
        output_path=output_path,
        voice=voice,
        speed=speed,
        language=language,
    )

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ {output_path.name} ({size_mb:.1f} MB)")
    return output_path


def _run_conversion(
    engine: TTSInferenceEngine,
    work: list,            # [(ci, title, text), …]
    total_words: int,
    book_title: str,
    author: str,
    coverdata: Optional[dict],
    tmpd: Path,
    output_path: Path,
    voice: str = "af_heart",
    speed: float = 1.0,
    language: str | None = None,
) -> None:
    n_work = len(work)
    wav_path = str(tmpd / "full.wav")
    gap = np.zeros(int(round(SR * 0.2)), dtype=np.int16)
    gap_len = gap.shape[0]

    book_bar = tqdm(
        total=total_words,
        desc=f"  {book_title[:50]}",
        unit="words",
        bar_format="  {desc} |{bar}| {percentage:3.0f}% {n_fmt}/{total_fmt} words [{elapsed}<{remaining}]",
        dynamic_ncols=True,
    )

    # ── Stream all chapters into a single WAV ─────────────────
    chapters_ff: list[dict] = []
    cursor = 0          # total samples written so far
    first_chunk = True   # suppress gap before the very first chunk

    with sf.SoundFile(wav_path, mode='w', samplerate=SR, channels=1,
                      subtype='PCM_16') as wf:
        for pos, (ci, title, text) in enumerate(work, 1):
            ch_words = len(text.split())
            ch_label = title if len(title) <= 40 else title[:37] + "…"
            book_bar.set_postfix_str(f"… ch {pos}/{n_work} {ch_label}")

            ch_start = cursor

            for chunk in engine.generate_chunks(
                text=text,
                voice=voice,
                speed=speed,
                language=language,
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
            duration_s = (ch_end - ch_start) / SR
            book_bar.update(ch_words)
            book_bar.set_postfix_str(f"✓ {ch_label} ({duration_s:.0f}s)")

            # Merge untitled chapters into the previous marker
            ch_title = (title or "").strip()
            if ch_title == "" and chapters_ff:
                chapters_ff[-1]["end_ms"] = int((ch_end * 1000) // SR)
            else:
                chapters_ff.append({
                    "title": ch_title or f"Chapter {len(chapters_ff)+1}",
                    "start_ms": int((ch_start * 1000) // SR),
                    "end_ms": int((ch_end * 1000) // SR),
                })

    del work, gap
    book_bar.close()

    total_duration = cursor / SR
    print(f"  Total audio: {total_duration/60:.1f} min, {len(chapters_ff)} chapters")

    # ── Write ffmetadata ──────────────────────────────────────
    meta_path = tmpd / "chapters.ffmeta"
    _write_ffmetadata(str(meta_path), book_title, author, chapters_ff)

    # ── Optional cover ────────────────────────────────────────
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

    # ── ffmpeg encode (single WAV → AAC M4B) ─────────────────
    print(f"  Encoding M4B …", flush=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", wav_path,
        "-f", "ffmetadata", "-i", str(meta_path),
        *cover_input_args,
        "-map", "0:a",
        *cover_output_map,
        "-map_metadata", "1", "-map_chapters", "1",
        "-threads", "auto",
        "-ar", "24000", "-c:a", "aac", "-b:a", AAC_BITRATE,
        str(output_path),
    ]
    subprocess.run(cmd, check=True)

    # ── Cleanup temp ──────────────────────────────────────────
    shutil.rmtree(tmpd, ignore_errors=True)
