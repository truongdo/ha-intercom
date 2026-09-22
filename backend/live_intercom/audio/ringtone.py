from __future__ import annotations

import numpy as np

from ..protocol import FRAME_BYTES, RATE

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


class RingtoneSource:
    """Cycles through a precomputed ring cadence, one FRAME_BYTES chunk at a time, looping forever."""

    def __init__(self) -> None:
        self._pos = 0

    def next_frame(self) -> bytes:
        end = self._pos + FRAME_BYTES
        if end <= CYCLE_BYTES:
            frame = _CYCLE[self._pos : end]
            self._pos = end % CYCLE_BYTES
        else:
            wrap = end - CYCLE_BYTES
            frame = _CYCLE[self._pos :] + _CYCLE[:wrap]
            self._pos = wrap
        return frame
