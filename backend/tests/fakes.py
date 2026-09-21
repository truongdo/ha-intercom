from __future__ import annotations

import time
from typing import Callable


class FakeDevice:
    """In-memory AudioDevice. Tests drive the mic and inspect the speaker side."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._on_mic: Callable[[bytes], None] | None = None
        self._pull: Callable[[], bytes] | None = None
        self._on_error: Callable[[str], None] | None = None

    def start(self, on_mic_frame, pull_speaker_frame, on_error) -> None:
        self._on_mic, self._pull, self._on_error = on_mic_frame, pull_speaker_frame, on_error
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def emit_mic(self, frame: bytes) -> None:
        assert self._on_mic is not None
        self._on_mic(frame)

    def pull_speaker(self) -> bytes:
        assert self._pull is not None
        return self._pull()

    def fail(self, reason: str) -> None:
        assert self._on_error is not None
        self._on_error(reason)


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
