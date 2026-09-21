from __future__ import annotations

import threading
from collections import deque

from ..protocol import SILENCE


class JitterBuffer:
    """Thread-safe bounded FIFO of audio frames; underrun yields silence."""

    def __init__(self, max_frames: int):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self._max = max_frames
        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()

    def push(self, frame: bytes) -> None:
        with self._lock:
            if len(self._frames) >= self._max:
                self._frames.popleft()
            self._frames.append(frame)

    def pop(self) -> bytes:
        with self._lock:
            return self._frames.popleft() if self._frames else SILENCE

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
