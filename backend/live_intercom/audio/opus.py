"""Opus at the WebSocket edge, via ctypes over the system libopus (no Python dependency).

Everything behind this module (ALSA, echo canceller, jitter buffer) stays 16 kHz mono int16
PCM in FRAME_SAMPLES frames; see protocol.py.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading

from ..protocol import CHANNELS, FRAME_BYTES, FRAME_SAMPLES, RATE

log = logging.getLogger(__name__)

APPLICATION_VOIP = 2048
_SET_BITRATE = 4002
_SET_VBR = 4006
_SET_COMPLEXITY = 4010
_SET_DTX = 4016

BITRATE = 20000
# Measured on the Orange Pi PC Plus: encode + decode at complexity 5 costs 8.6 % of one core.
COMPLEXITY = 5
MAX_PACKET_BYTES = 1500

# find_library() doesn't search Homebrew, which is where the Mac dev setup gets libopus.
_CANDIDATES = ("libopus.so.0", "/opt/homebrew/lib/libopus.0.dylib")

_lib: ctypes.CDLL | None = None


class OpusError(Exception):
    pass


def load() -> bool:
    """Load libopus once. False (with a warning) if it isn't installed; callers then use PCM."""
    global _lib
    if _lib is not None:
        return True
    for name in (ctypes.util.find_library("opus"), *_CANDIDATES):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            _declare(lib)
        except (OSError, AttributeError):
            continue
        _lib = lib
        return True
    log.warning("libopus not found; calls will use raw PCM")
    return False


def _declare(lib: ctypes.CDLL) -> None:
    c_void_p, c_int, c_int32, c_char_p = ctypes.c_void_p, ctypes.c_int, ctypes.c_int32, ctypes.c_char_p
    lib.opus_encoder_create.restype = c_void_p
    lib.opus_encoder_create.argtypes = [c_int32, c_int, c_int, ctypes.POINTER(c_int)]
    lib.opus_encoder_destroy.restype = None
    lib.opus_encoder_destroy.argtypes = [c_void_p]
    # Variadic in C: the value argument is passed as an extra c_int32 at the call site.
    lib.opus_encoder_ctl.restype = c_int
    lib.opus_encoder_ctl.argtypes = [c_void_p, c_int]
    lib.opus_encode.restype = c_int32
    lib.opus_encode.argtypes = [c_void_p, c_char_p, c_int, c_char_p, c_int32]
    lib.opus_decoder_create.restype = c_void_p
    lib.opus_decoder_create.argtypes = [c_int32, c_int, ctypes.POINTER(c_int)]
    lib.opus_decoder_destroy.restype = None
    lib.opus_decoder_destroy.argtypes = [c_void_p]
    lib.opus_decode.restype = c_int
    lib.opus_decode.argtypes = [c_void_p, c_char_p, c_int32, c_char_p, c_int, c_int]
    lib.opus_strerror.restype = c_char_p
    lib.opus_strerror.argtypes = [c_int]


def _require() -> ctypes.CDLL:
    if _lib is None and not load():
        raise OpusError("libopus is not available")
    assert _lib is not None
    return _lib


def _check(code: int) -> int:
    if code < 0:
        assert _lib is not None
        raise OpusError(_lib.opus_strerror(code).decode())
    return code


class OpusEncoder:
    """One caller-bound stream: 16 kHz mono, VOIP, 20 kbps VBR, no DTX (see COMPLEXITY)."""

    def __init__(self) -> None:
        lib = _require()
        err = ctypes.c_int()
        self._state = lib.opus_encoder_create(RATE, CHANNELS, APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self._state:
            self._state = None
            raise OpusError(f"opus_encoder_create failed: {err.value}")
        # No DTX: the server's idle timeout and jitter buffer both expect a steady 50 frames/s.
        for request, value in ((_SET_BITRATE, BITRATE), (_SET_VBR, 1), (_SET_COMPLEXITY, COMPLEXITY), (_SET_DTX, 0)):
            _check(lib.opus_encoder_ctl(self._state, request, ctypes.c_int32(value)))
        self._out = ctypes.create_string_buffer(MAX_PACKET_BYTES)

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"expected {FRAME_BYTES} bytes of PCM, got {len(pcm)}")
        if not self._state:
            raise OpusError("encoder is closed")
        n = _check(_require().opus_encode(self._state, pcm, FRAME_SAMPLES, self._out, MAX_PACKET_BYTES))
        return ctypes.string_at(self._out, n)

    def close(self) -> None:
        if getattr(self, "_state", None):
            _require().opus_encoder_destroy(self._state)
            self._state = None

    def __del__(self) -> None:
        self.close()


class OpusDecoder:
    """One caller-bound stream. decode() runs on the event loop and conceal() on the ALSA
    callback thread, so both hold a lock around the shared libopus state."""

    def __init__(self) -> None:
        lib = _require()
        err = ctypes.c_int()
        self._state = lib.opus_decoder_create(RATE, CHANNELS, ctypes.byref(err))
        if err.value != 0 or not self._state:
            self._state = None
            raise OpusError(f"opus_decoder_create failed: {err.value}")
        self._pcm = ctypes.create_string_buffer(FRAME_BYTES)
        self._lock = threading.Lock()

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            raise OpusError("empty packet")  # libopus would treat it as lost and conceal it
        return self._decode(packet)

    def conceal(self) -> bytes:
        """Packet-loss concealment: one plausible frame continuing the audio decoded so far."""
        return self._decode(None)

    def _decode(self, packet: bytes | None) -> bytes:
        with self._lock:
            if not self._state:
                raise OpusError("decoder is closed")
            size = len(packet) if packet is not None else 0
            n = _check(_require().opus_decode(self._state, packet, size, self._pcm, FRAME_SAMPLES, 0))
            # libopus is lenient (arbitrary bytes often "decode"); anything that isn't exactly
            # one 20 ms frame is not a packet this protocol sends.
            if n != FRAME_SAMPLES:
                raise OpusError(f"decoded {n} samples, expected {FRAME_SAMPLES}")
            return ctypes.string_at(self._pcm, FRAME_BYTES)

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            if self._state:
                _require().opus_decoder_destroy(self._state)
                self._state = None

    def __del__(self) -> None:
        self.close()
