"""Shared text preprocessing helpers for Kokoro-like engines."""

from __future__ import annotations

import re


_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """Split text into sentence-like fragments."""
    parts = _SPLIT_RE.split(text)
    return [part.strip() for part in parts if part.strip()]


def group_sentences(sentences: list[str], target_chars: int) -> list[str]:
    """Merge short fragments into larger blocks for better throughput."""
    grouped: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        sentence_len = len(sentence)
        extra = sentence_len + (1 if current else 0)
        if current and current_len + extra > target_chars:
            grouped.append("\n".join(current))
            current = [sentence]
            current_len = sentence_len
        else:
            current.append(sentence)
            current_len += extra
    if current:
        grouped.append("\n".join(current))
    return grouped