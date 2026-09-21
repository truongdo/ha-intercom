from __future__ import annotations

import threading
from collections import deque

from ..protocol import SILENCE


class JitterBuffer:
    """Thread-safe bounded FIFO of audio frames; underrun yields silence.

    Audio is withheld until the buffer holds ``prime_frames`` frames, and again after every
    underrun, so the buffer actually provides its depth as protection against late packets.
    """

    def __init__(self, max_frames: int, prime_frames: int | None = None):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self._max = max_frames
        self._prime = min(max_frames, prime_frames if prime_frames is not None else max_frames // 2 + 1)
        self._priming = True
        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()

    def push(self, frame: bytes) -> None:
        with self._lock:
            if len(self._frames) >= self._max:
                self._frames.popleft()
            self._frames.append(frame)
            if len(self._frames) >= self._prime:
                self._priming = False

    def pop(self) -> bytes:
        with self._lock:
            if self._priming:
                return SILENCE
            if not self._frames:
                self._priming = True  # underrun: rebuild depth before playing again
                return SILENCE
            return self._frames.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
