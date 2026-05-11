"""Kokoro ONNX backend for the audiobook pipeline."""

from __future__ import annotations

import ctypes
import gc
import os
import re
from pathlib import Path

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

from ._kokoro_text import group_sentences, split_sentences
from .base import TTSInferenceEngine


DEFAULT_MODEL_PATH = os.getenv("KOKORO_ONNX_MODEL", "onnx-model/kokoro-v1.0.fp16-gpu.onnx")
DEFAULT_VOICES_PATH = os.getenv("KOKORO_ONNX_VOICES", "onnx-model/voices-v1.0.bin")
DEFAULT_LANGUAGE = os.getenv("KOKORO_LANG", "en-us")
DEFAULT_GROUP_TARGET_CHARS = int(os.getenv("KOKORO_GROUP_TARGET_CHARS", "900"))


class OnnxTTSInferenceEngine(TTSInferenceEngine):
    """Singleton Kokoro-ONNX inference engine."""

    _instance: "OnnxTTSInferenceEngine | None" = None

    def __new__(cls, *args, **kwargs) -> "OnnxTTSInferenceEngine":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        voices_path: str | Path = DEFAULT_VOICES_PATH,
        default_language: str = DEFAULT_LANGUAGE,
        group_target_chars: int = DEFAULT_GROUP_TARGET_CHARS,
    ) -> None:
        if self._initialized:
            return
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.default_language = default_language
        self.group_target_chars = group_target_chars
        self._pipeline: Kokoro | None = None
        self._initialized = True

    def start(self) -> None:
        if self._pipeline is None:
            self._load_pipeline()

    def shutdown(self) -> None:
        if self._pipeline is None:
            return
        del self._pipeline
        self._pipeline = None
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    def generate_chunks(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        language: str | None = None,
    ):
        self.start()
        selected_language = language or self.default_language
        sentences = split_sentences(text)
        grouped = group_sentences(sentences, self.group_target_chars)
        for block in grouped:
            if not re.search(r"[A-Za-z0-9]", block):
                continue
            audio, _sample_rate = self._pipeline.create(
                block,
                voice=voice,
                speed=speed,
                lang=selected_language,
            )
            arr = np.asarray(audio, dtype=np.float32).ravel()
            if arr.size == 0:
                continue
            np.clip(arr, -1.0, 1.0, out=arr)
            chunk = (arr * 32767.0).astype(np.int16)
            del arr
            yield chunk
            del chunk

    def _load_pipeline(self) -> None:
        providers = ort.get_available_providers()
        preferred = (
            "MIGraphXExecutionProvider",
            "ROCMExecutionProvider",
            "CPUExecutionProvider",
        )
        selected_provider = next((provider for provider in preferred if provider in providers), None)
        if selected_provider is None:
            raise RuntimeError(f"No compatible ONNX provider found. Got: {providers}")

        if selected_provider in ("MIGraphXExecutionProvider", "ROCMExecutionProvider"):
            os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")
            os.environ.setdefault("ROCR_VISIBLE_DEVICES", "0")
        os.environ["ONNX_PROVIDER"] = selected_provider

        if not self.model_path.exists() or not self.voices_path.exists():
            raise FileNotFoundError(
                "Kokoro ONNX files not found. Expected model/voices at "
                f"{self.model_path} and {self.voices_path}. "
                "Override with KOKORO_ONNX_MODEL and KOKORO_ONNX_VOICES."
            )

        self._pipeline = Kokoro(str(self.model_path), str(self.voices_path))