"""Kokoro-shaped TTS abstraction used by the audiobook pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

import numpy as np


class TTSInferenceEngine(ABC):
    """Minimal TTS contract for Kokoro-like backends."""

    @abstractmethod
    def start(self) -> None:
        """Initialize the backend if it has not been loaded yet."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release backend resources if supported."""

    @abstractmethod
    def generate_chunks(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        language: str | None = None,
    ) -> Iterator[np.ndarray]:
        """Yield 1-D int16 audio chunks for the provided text."""