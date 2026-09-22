from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from ..protocol import FRAME_BYTES, RATE

log = logging.getLogger(__name__)

TONE_HZ = 440.0
RING_ON_S = 1.5
RING_OFF_S = 3.0


def _generate_cycle() -> bytes:
    on_samples = int(RATE * RING_ON_S)
    off_samples = int(RATE * RING_OFF_S)
    t = np.arange(on_samples) / RATE
    tone = (np.sin(2 * np.pi * TONE_HZ * t) * 0.3 * 32767).astype(np.int16)
    silence = np.zeros(off_samples, dtype=np.int16)
    return np.concatenate([tone, silence]).tobytes()


_CYCLE = _generate_cycle()
CYCLE_BYTES = len(_CYCLE)


def _load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getframerate() != RATE or wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
            raise ValueError(
                f"must be {RATE} Hz mono 16-bit PCM, got {wav_file.getframerate()} Hz, "
                f"{wav_file.getnchannels()} channel(s), {wav_file.getsampwidth() * 8}-bit"
            )
        frames = wav_file.readframes(wav_file.getnframes())
    if not frames:
        raise ValueError("file is empty")
    return frames


class RingtoneSource:
    """Cycles through a ring cadence, one FRAME_BYTES chunk at a time, looping forever.

    Loads `ringtone_file` (16 kHz mono 16-bit PCM WAV) if given; falls back to the
    built-in synthesized tone if it's unset, missing, or not in the expected format —
    a misconfigured ringtone must never break the call itself.
    """

    def __init__(self, ringtone_file: Path | None = None) -> None:
        self._cycle = _CYCLE
        if ringtone_file is not None:
            try:
                self._cycle = _load_wav(ringtone_file)
            except (OSError, wave.Error, ValueError):
                log.exception("cannot load ringtone_file %s; using the built-in tone", ringtone_file)
        self._pos = 0

    def next_frame(self) -> bytes:
        cycle = self._cycle
        end = self._pos + FRAME_BYTES
        if end <= len(cycle):
            frame = cycle[self._pos : end]
            self._pos = end % len(cycle)
        else:
            wrap = end - len(cycle)
            frame = cycle[self._pos :] + cycle[:wrap]
            self._pos = wrap
        return frame
