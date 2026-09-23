from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np

from ..protocol import FRAME_SAMPLES, SILENCE

log = logging.getLogger(__name__)

# ~8 ms at 16 kHz: long enough to mask the jump between real audio and silence (or between
# two unrelated stretches of audio), short enough to stay inaudible as an effect in its own right.
FADE_SAMPLES = min(128, FRAME_SAMPLES)

# Raised cosine 0 -> 1: unlike a linear ramp, its slope is also zero at both ends, so the ramp
# has no corners that are themselves audible as a soft click (same curve as playback-worklet.js).
_RAMP = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, FADE_SAMPLES, endpoint=True))

# How long an underrun is bridged with codec concealment before fading to silence: long
# enough to cover a typical late packet, short enough that PLC's synthetic audio (which
# degrades the longer it extrapolates) never becomes noticeable in its own right.
MAX_CONCEAL_FRAMES = 2


def _fade_out(from_sample: int) -> bytes:
    out = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    out[:FADE_SAMPLES] = np.round(from_sample * (1.0 - _RAMP)).astype(np.int16)
    return out.tobytes()


def _crossfade(from_sample: int, frame: bytes) -> bytes:
    """Ramp from ``from_sample`` into ``frame``'s own content (from 0 = a plain fade-in)."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64).copy()
    samples[:FADE_SAMPLES] = samples[:FADE_SAMPLES] * _RAMP + from_sample * (1.0 - _RAMP)
    return np.round(samples).astype(np.int16).tobytes()


class JitterBuffer:
    """Thread-safe bounded FIFO of audio frames; underrun yields silence.

    Audio is withheld until the buffer holds ``prime_frames`` frames, and again after every
    underrun, so the buffer actually provides its depth as protection against late packets.

    The first silent frame after real audio, and the first real frame after silence, are
    ramped rather than cut, so an underrun (or the buffer priming) doesn't sound like a click.

    On overflow the oldest frames are dropped back down to the priming depth in one go, and
    the next frame played crossfades from the last sample actually played. Over a bursty link
    (TCP on cellular stalls, then delivers everything at once) trimming one frame per push
    instead would splice on every frame of the burst.

    When a ``conceal`` callable is given (the Opus decoder's packet-loss concealment), an
    underrun mid-audio first plays up to MAX_CONCEAL_FRAMES frames from it, then fades out
    from the last concealed sample as usual. Real audio resuming during concealment
    crossfades in from the concealed audio.
    """

    def __init__(
        self,
        max_frames: int,
        prime_frames: int | None = None,
        conceal: Callable[[], bytes] | None = None,
    ):
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
        self._spliced = False
        self._underruns = 0
        self._dropped = 0
        self._received = 0
        self._gap_frames = 0
        self._conceal = conceal
        self._conceal_run = 0
        self._concealed = 0

    def push(self, frame: bytes) -> None:
        with self._lock:
            self._frames.append(frame)
            self._received += 1
            if len(self._frames) > self._max:
                while len(self._frames) > self._prime:
                    self._frames.popleft()
                    self._dropped += 1
                self._spliced = True
            if len(self._frames) >= self._prime:
                self._priming = False

    def pop(self) -> bytes:
        with self._lock:
            if self._priming or not self._frames:
                if not self._frames:
                    if not self._priming:
                        self._underruns += 1
                    self._priming = True  # underrun: rebuild depth before playing again
                if self._had_real:
                    self._gap_frames += 1
                if self._had_real and not self._silent and self._conceal_run < MAX_CONCEAL_FRAMES:
                    concealed = self._try_conceal()
                    if concealed is not None:
                        self._conceal_run += 1
                        self._concealed += 1
                        self._spliced = True  # audio resuming now must crossfade from this
                        self._last_sample = int(np.frombuffer(concealed, dtype=np.int16)[-1])
                        return concealed
                entering_gap = self._had_real and not self._silent
                frame = _fade_out(self._last_sample) if entering_gap else SILENCE
                self._silent = True
                return frame
            frame = self._frames.popleft()
            if self._had_real and self._silent:
                frame = _crossfade(0, frame)
            elif self._had_real and self._spliced:
                frame = _crossfade(self._last_sample, frame)
            self._spliced = False
            self._silent = False
            self._had_real = True
            self._conceal_run = 0
            self._last_sample = int(np.frombuffer(frame, dtype=np.int16)[-1])
            return frame

    def _try_conceal(self) -> bytes | None:
        if self._conceal is None:
            return None
        try:
            return self._conceal()
        except Exception:
            # e.g. the decoder was already closed because a hung device stop left this
            # callback running: fall back to the plain fade rather than killing audio.
            log.exception("packet-loss concealment failed")
            return None

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "received_frames": self._received,
                "gap_frames": self._gap_frames,  # silence played mid-call while waiting for audio
                "underruns": self._underruns,
                "dropped_frames": self._dropped,
                "concealed_frames": self._concealed,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
