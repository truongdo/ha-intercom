from __future__ import annotations

from typing import Protocol

from ..protocol import FRAME_SAMPLES, RATE

# 2048 samples at 16 kHz is 128 ms of echo tail; tune on the real device.
FILTER_LENGTH = 2048


class EchoCanceller(Protocol):
    def process(self, near: bytes, far: bytes) -> bytes: ...


class NullCanceller:
    def process(self, near: bytes, far: bytes) -> bytes:
        return near


class SpeexCanceller:
    def __init__(self) -> None:
        try:
            from speexdsp import EchoCanceller as _Speex
        except ImportError as exc:
            raise RuntimeError(
                "echo_cancel = 'speex' needs the optional 'speexdsp' package "
                "(pip install speexdsp; requires libspeexdsp-dev to build)"
            ) from exc
        self._ec = _Speex.create(FRAME_SAMPLES, FILTER_LENGTH, RATE)

    def process(self, near: bytes, far: bytes) -> bytes:
        return self._ec.process(near, far)


def make_canceller(mode: str) -> EchoCanceller:
    if mode == "off":
        return NullCanceller()
    if mode == "speex":
        return SpeexCanceller()
    raise ValueError(f"unknown echo_cancel mode {mode!r}")
