#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT


OLLAMA_URL = "http://localhost:11434/api/generate"


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav"]):
        tag.decompose()

    text = soup.get_text("\n")
    text = unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    # Crude estimate for local chunking decisions only.
    # Qwen won't match exactly; this is just to avoid enormous prompts.
    return max(1, len(text) // 4)


def split_text_by_size(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current = []

    current_len = 0
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue

        extra = len(p) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            chunks.append("\n\n".join(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += extra

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def parse_epub(epub_path: Path) -> List[Dict[str, Any]]:
    book = epub.read_epub(str(epub_path))
    chapters = []

    # Use the spine to get items in correct reading order
    items_by_id = {item.get_id(): item for item in book.get_items()}

    index = 1
    for spine_id, _linear in book.spine:
        item = items_by_id.get(spine_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue

        try:
            raw = item.get_body_content().decode("utf-8", errors="ignore")
        except Exception:
            raw = item.content.decode("utf-8", errors="ignore")

        text = clean_text(raw)
        if len(text) < 300:
            continue

        soup = BeautifulSoup(raw, "html.parser")
        title_tag = soup.find(["h1", "h2", "title"])
        title = title_tag.get_text(" ", strip=True) if title_tag else f"Chapter {index}"

        chapters.append({
            "index": index,
            "id": item.get_name(),
            "title": title,
            "text": text,
        })
        index += 1

    return chapters


def ollama_generate(
    model: str,
    prompt: str,
    num_ctx: int = 65536,
    temperature: float = 0.1,
    repeat_penalty: float = 1.3,
    timeout: int = 1800,
    stream: bool = False,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "repeat_last_n": 256,
        },
    }

    if not stream:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "").strip()

    # Streaming mode: print tokens to stderr as they arrive
    parts = []
    with requests.post(OLLAMA_URL, json=payload, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            token = chunk.get("response", "")
            if token:
                parts.append(token)
                sys.stderr.write(token)
                sys.stderr.flush()
    sys.stderr.write("\n")
    return "".join(parts).strip()


def summarize_large_chapter(
    model: str,
    chapter_title: str,
    chapter_text: str,
    num_ctx: int,
    max_input_chars_per_pass: int,
    stream: bool = False,
    temperature: float = 0.1,
    repeat_penalty: float = 1.3,
) -> str:
    chunks = split_text_by_size(chapter_text, max_input_chars_per_pass)

    if len(chunks) == 1:
        prompt = f"""
You are producing a very detailed chapter recap for a novel.

Task:
Write a detailed recap of the chapter below.

Requirements:
- Preserve the chronological order of events.
- Include important character actions, motivations, conflicts, reveals, and consequences.
- Mention notable worldbuilding details that matter to the plot.
- Mention emotional shifts and interpersonal dynamics.
- Do not write fluff, but be thorough.
- Do not add information not present in the text.
- Write in clear prose with section headings:
  1. Chapter overview
  2. Main events
  3. Character developments
  4. Important details and reveals
  5. Chapter ending / cliffhangers

Chapter title: {chapter_title}

Chapter text:
{chapter_text}
""".strip()
        return ollama_generate(model=model, prompt=prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)

    partials = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prompt = f"""
You are producing a partial recap for one section of a novel chapter.

Task:
Summarize this section thoroughly and factually.

Requirements:
- Preserve chronological order.
- Keep names, places, and important objects accurate.
- Capture plot beats, motivations, reveals, and consequences.
- Write enough detail that this can later be merged into a full chapter recap.

Chapter title: {chapter_title}
Section: {i}/{total}

Section text:
{chunk}
""".strip()
        partial = ollama_generate(model=model, prompt=prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)
        partials.append(partial)

    merge_prompt = f"""
You are merging partial summaries into one detailed chapter recap.

Task:
Combine the section summaries below into a single coherent and detailed chapter recap.

Requirements:
- Preserve the chapter's chronology.
- Remove repetition.
- Keep all important plot points, character developments, reveals, and ending beats.
- Use section headings:
  1. Chapter overview
  2. Main events
  3. Character developments
  4. Important details and reveals
  5. Chapter ending / cliffhangers

Chapter title: {chapter_title}

Partial summaries:
{"\n\n---\n\n".join(partials)}
""".strip()

    return ollama_generate(model=model, prompt=merge_prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)


def build_final_summary(
    model: str,
    chapter_summaries: List[Dict[str, Any]],
    num_ctx: int,
    max_merge_chars: int,
    stream: bool = False,
    temperature: float = 0.1,
    repeat_penalty: float = 1.3,
) -> str:
    blocks = []
    for ch in chapter_summaries:
        blocks.append(f"# {ch['title']}\n\n{ch['summary']}")

    combined = "\n\n====================\n\n".join(blocks)
    groups = split_text_by_size(combined, max_merge_chars)

    if len(groups) == 1:
        prompt = f"""
You are producing a long, detailed recap of an entire novel from chapter recaps.

Task:
Write a detailed overall recap of the book based on the chapter recaps below.

Requirements:
- Be long and comprehensive.
- Preserve the full arc of the story from beginning to end.
- Cover major plot developments, character arcs, turning points, conflicts, revelations, and resolution.
- Explain how relationships evolve over time.
- Mention important worldbuilding elements only where relevant to the story.
- Include the ending in detail.
- Do not invent anything not supported by the chapter recaps.
- Organize the response with these headings:
  1. Overall premise
  2. Story progression
  3. Major character arcs
  4. Key revelations and turning points
  5. Themes and recurring conflicts
  6. Ending and final state of the story

Chapter recaps:
{combined}
""".strip()
        return ollama_generate(model=model, prompt=prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)

    partial_book_summaries = []
    total = len(groups)
    for i, group in enumerate(groups, start=1):
        prompt = f"""
You are summarizing one segment of chapter recaps for a novel.

Task:
Produce a detailed partial overall recap for this subset of chapter summaries.

Requirements:
- Preserve chronology within this subset.
- Include major plot beats, character development, reveals, and consequences.
- Keep it detailed enough to be merged later into a full-book recap.

Subset {i}/{total}

Chapter recaps:
{group}
""".strip()
        partial_book_summaries.append(
            ollama_generate(model=model, prompt=prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)
        )

    merge_prompt = f"""
You are merging partial book recaps into one final long recap.

Task:
Write a long, detailed overall recap of the novel based on the partial overall recaps below.

Requirements:
- Preserve beginning-to-end chronology.
- Merge repeated points cleanly.
- Cover plot progression, character arcs, turning points, reveals, themes, and ending in detail.
- Use these headings:
  1. Overall premise
  2. Story progression
  3. Major character arcs
  4. Key revelations and turning points
  5. Themes and recurring conflicts
  6. Ending and final state of the story

Partial overall recaps:
{"\n\n---\n\n".join(partial_book_summaries)}
""".strip()

    return ollama_generate(model=model, prompt=merge_prompt, num_ctx=num_ctx, stream=stream, temperature=temperature, repeat_penalty=repeat_penalty)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub_file", type=Path)
    parser.add_argument("--model", required=True, help="Ollama model name")
    parser.add_argument("--outdir", type=Path, default=Path("recap_output"))
    parser.add_argument("--num-ctx", type=int, default=65536)
    parser.add_argument(
        "--max-input-chars-per-pass",
        type=int,
        default=120000,
        help="Approx safe input size per chapter/chunk prompt",
    )
    parser.add_argument(
        "--max-merge-chars",
        type=int,
        default=160000,
        help="Approx safe size when merging chapter recaps into final recap",
    )
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature (default: 0.1)")
    parser.add_argument("--repeat-penalty", type=float, default=1.3, help="Repetition penalty (default: 1.3)")
    parser.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="Stream LLM output to stderr so you can inspect it live",
    )
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)

    chapters = parse_epub(args.epub_file)
    if not chapters:
        print("No usable chapters found.", file=sys.stderr)
        return 1

    print(f"Found {len(chapters)} chapters")

    chapter_summaries = []
    for ch in chapters:
        print(f"Summarizing chapter {ch['index']}: {ch['title']}", flush=True)
        print(f"  approx input tokens: {estimate_tokens(ch['text'])}", flush=True)

        summary = summarize_large_chapter(
            model=args.model,
            chapter_title=ch["title"],
            chapter_text=ch["text"],
            num_ctx=args.num_ctx,
            max_input_chars_per_pass=args.max_input_chars_per_pass,
            stream=args.stream,
            temperature=args.temperature,
            repeat_penalty=args.repeat_penalty,
        )

        chapter_record = {
            "index": ch["index"],
            "title": ch["title"],
            "source_id": ch["id"],
            "summary": summary,
        }
        chapter_summaries.append(chapter_record)

        chapter_file = args.outdir / f"{ch['index']:03d}_{safe_filename(ch['title'])}.md"
        chapter_file.write_text(
            f"# {ch['title']}\n\n{summary}\n",
            encoding="utf-8",
        )

        if args.sleep > 0:
            time.sleep(args.sleep)

    json_file = args.outdir / "chapter_summaries.json"
    json_file.write_text(
        json.dumps(chapter_summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Building final recap...", flush=True)
    final_summary = build_final_summary(
        model=args.model,
        chapter_summaries=chapter_summaries,
        num_ctx=args.num_ctx,
        max_merge_chars=args.max_merge_chars,
        stream=args.stream,
        temperature=args.temperature,
        repeat_penalty=args.repeat_penalty,
    )

    final_file = args.outdir / "final_recap.md"
    final_file.write_text(final_summary, encoding="utf-8")

    print(f"Done.\nChapter summaries: {json_file}\nFinal recap: {final_file}")
    return 0


def safe_filename(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9._-]+", "_", name)
    return name[:80].strip("_") or "chapter"


if __name__ == "__main__":
    raise SystemExit(main())
