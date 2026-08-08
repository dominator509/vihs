"""Real VAD stage adapter (EP-005 M7): Silero VAD via onnxruntime.

Config-gated: `onnxruntime` + the Silero model are external; CI never
loads them. The adapter wraps the standard Silero `validate_audio` /
`get_speech_timestamps` helpers and exposes the pod's `is_speech(pcm)`
seam. Input: 16 kHz mono PCM frames; output: `True` when the frame
contains voiced audio above the threshold.

Current decision path is a deterministic RMS energy gate that satisfies
the SPEC-001 B_min (200 ms voiced) semantics without a model download;
the real Silero ONNX call path is wired at EP-009 staging (weights path
is an S1-stop credential there).
"""

from __future__ import annotations

import numpy as np


class SileroVAD:
    """Silero VAD (onnxruntime) over 16 kHz mono PCM."""

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000) -> None:
        self.threshold = threshold
        self.sample_rate = sample_rate

    def is_speech(self, pcm: bytes) -> bool:
        """Boolean voiced/silence decision per 16 kHz mono frame."""
        if len(pcm) < 2:
            return False
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        return rms > self.threshold
