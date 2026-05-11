"""Ordinary Kokoro backend for the audiobook pipeline."""

from __future__ import annotations

import gc
import re

import numpy as np

from ._kokoro_text import group_sentences, split_sentences
from .base import TTSInferenceEngine


DEFAULT_LANGUAGE = "a"
DEFAULT_GROUP_TARGET_CHARS = 900
GROUPING = False


class KokoroTTSInferenceEngine(TTSInferenceEngine):
    """Singleton PyTorch Kokoro inference engine."""

    _instance: "KokoroTTSInferenceEngine | None" = None

    def __new__(cls, *args, **kwargs) -> "KokoroTTSInferenceEngine":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        default_language: str = DEFAULT_LANGUAGE,
        group_target_chars: int = DEFAULT_GROUP_TARGET_CHARS,
        repo_id: str = "hexgrad/Kokoro-82M",
        device: str = "cuda",
    ) -> None:
        if self._initialized:
            return
        self.default_language = default_language
        self.group_target_chars = group_target_chars
        self.repo_id = repo_id
        self.device = device
        self._model = None
        self._pipeline = None
        self._initialized = True

    def start(self) -> None:
        if self._pipeline is not None:
            return
        from kokoro import KModel, KPipeline

        self._model = KModel(repo_id=self.repo_id).to(self.device).eval()
        self._pipeline = KPipeline(
            lang_code=self.default_language,
            repo_id=self.repo_id,
            model=self._model,
            device=self.device,
        )

    def shutdown(self) -> None:
        if self._pipeline is None and self._model is None:
            return
        self._pipeline = None
        self._model = None
        gc.collect()

    def generate_chunks(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        language: str | None = None,
    ):
        # language is fixed at construction via default_language; the KPipeline
        # G2P processor cannot be repointed after init, so per-call overrides
        # are ignored. To use a different language, construct a new engine
        # instance with the desired default_language.
        self.start()
        sentences = split_sentences(text)
        blocks = group_sentences(sentences, self.group_target_chars) if GROUPING else sentences
        for block in blocks:
            if not re.search(r"[A-Za-z0-9]", block):
                continue
            for _gs, _ps, audio in self._pipeline(
                block,
                voice=voice,
                speed=speed,
                split_pattern=r"\n+",
            ):
                arr = np.asarray(audio, dtype=np.float32).ravel()
                if arr.size == 0:
                    continue
                np.clip(arr, -1.0, 1.0, out=arr)
                chunk = (arr * 32767.0).astype(np.int16)
                del arr
                yield chunk
                del chunk

