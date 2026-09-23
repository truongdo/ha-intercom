from __future__ import annotations

import threading
from collections import deque

import numpy as np

from ..protocol import FRAME_SAMPLES, SILENCE

# ~4 ms at 16 kHz: long enough that a linear ramp masks the jump between real audio and
# silence, short enough to stay inaudible as an effect in its own right.
FADE_SAMPLES = min(64, FRAME_SAMPLES)


def _fade_out(from_sample: int) -> bytes:
    out = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    ramp = np.linspace(from_sample, 0, FADE_SAMPLES, endpoint=True)
    out[:FADE_SAMPLES] = np.round(ramp).astype(np.int16)
    return out.tobytes()


def _fade_in(frame: bytes) -> bytes:
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64).copy()
    ramp = np.linspace(0.0, 1.0, FADE_SAMPLES, endpoint=True)
    samples[:FADE_SAMPLES] *= ramp
    return np.round(samples).astype(np.int16).tobytes()


class JitterBuffer:
    """Thread-safe bounded FIFO of audio frames; underrun yields silence.

    Audio is withheld until the buffer holds ``prime_frames`` frames, and again after every
    underrun, so the buffer actually provides its depth as protection against late packets.

    The first silent frame after real audio, and the first real frame after silence, are
    ramped rather than cut, so an underrun (or the buffer priming) doesn't sound like a click.
    """

    def __init__(self, max_frames: int, prime_frames: int | None = None):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self._max = max_frames
        self._prime = min(max_frames, prime_frames if prime_frames is not None else max_frames // 2 + 1)
        self._priming = True
        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()
        # Fade state: only engages once real audio has actually played, so startup silence
        # (before the first frame ever arrives) stays plain silence, not a ramp from zero.
        self._had_real = False
        self._silent = True
        self._last_sample = 0

    def push(self, frame: bytes) -> None:
        with self._lock:
            if len(self._frames) >= self._max:
                self._frames.popleft()
            self._frames.append(frame)
            if len(self._frames) >= self._prime:
                self._priming = False

    def pop(self) -> bytes:
        with self._lock:
            if self._priming or not self._frames:
                if not self._frames:
                    self._priming = True  # underrun: rebuild depth before playing again
                entering_gap = self._had_real and not self._silent
                frame = _fade_out(self._last_sample) if entering_gap else SILENCE
                self._silent = True
                return frame
            frame = self._frames.popleft()
            if self._had_real and self._silent:
                frame = _fade_in(frame)
            self._silent = False
            self._had_real = True
            self._last_sample = int(np.frombuffer(frame, dtype=np.int16)[-1])
            return frame

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
