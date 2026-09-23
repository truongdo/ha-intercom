# Opus Audio Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry call audio over the WebSocket as Opus (about 20 kbps) instead of raw PCM
(256 kbps) in both directions, with a PCM fallback negotiated per call.

**Architecture:** The codec lives only at the WebSocket edge.
- **Host:** a ctypes wrapper over the system libopus. It decodes caller packets on arrival,
  before the (unchanged, PCM) jitter buffer, and encodes host-mic frames just before sending.
- **Jitter buffer:** gains an optional Opus packet-loss-concealment hook.
- **Browser:** WASM libopus (`@evan/wasm`) encodes after the `Framer` and decodes before
  `int16ToFloat`.
- **Negotiation:** the client asks with `/ws?codec=opus`, and the server answers in
  `ready.codec`.

**Tech Stack:**
- Backend: Python 3.11+, Starlette, ctypes, libopus 1.5.2 (Debian `libopus0` on the host,
  Homebrew `opus` on the Mac), numpy, pytest.
- Frontend: TypeScript, Vite, vitest, `@evan/wasm` 0.0.95 (`target/opus/deno.js`).

**Spec:** `docs/superpowers/specs/2026-09-23-opus-codec-design.md`

## Global Constraints

- Opus settings: 16 kHz mono, 20 ms frames (320 samples = 640 bytes of PCM),
  `OPUS_APPLICATION_VOIP` (2048), 20 kbps VBR, complexity 5, **no DTX**. They're identical on
  host and browser.
- Wire format: each binary WebSocket message is exactly one frame: 640 bytes of int16 LE for
  `pcm`, or one raw Opus packet (no Ogg, no header) for `opus`.
- Codec names are the strings `"pcm"` and `"opus"`. `ready` always carries `"codec"`.
- The server picks `"opus"` only when the client sent `?codec=opus` **and** libopus loaded.
  Otherwise it picks `"pcm"`. The client follows `ready.codec`.
- An Opus decode is valid only when it yields exactly 320 samples. An empty packet is invalid.
  This applies on both host and browser.
- A bad received packet is dropped and counted, never fatal. Encode failure skips the frame.
  WASM load failure falls back to PCM.
- No new Python dependency (ctypes only). Pin the frontend dependency exactly:
  `"@evan/wasm": "0.0.95"`.
- `MAX_CONCEAL_FRAMES = 2` (40 ms of PLC per underrun). With `conceal=None` the jitter buffer
  behaves exactly as it does today.
- Existing PCM behaviour and tests stay green. The only existing assertions that change are the
  exact-dict checks of `ready_message()` and `JitterBuffer.stats()`.
- Tooling in this environment:
  - Backend tests: `cd backend && .venv/bin/pytest -q`. The baseline is 167 passed.
  - Frontend: the `npm`/`node` shell functions are broken (nvm lazy-load), so prefix commands
    with `PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH` or call that `npm` directly. The
    baseline is 8 tests passing.
  - libopus is at `/opt/homebrew/lib/libopus.0.dylib` on the Mac.
- Deploying to the host (`deploy/install.sh`) is outward-facing: **ask the owner before running
  it** (Task 6).

## Review Focus

1. **The host device hangs on stop, and the ALSA thread keeps calling `conceal()` after the
   decoder was closed.** The expected behaviour is no crash and a fade to silence. Pinned in
   Task 1 (`conceal()` after `close()` raises `OpusError`) and Task 2 (a raising `conceal`
   falls back to fading).
2. **A caller mutes the mic, so the browser sends digital silence.** With no DTX the stream must
   stay at 50 packets per second of non-empty packets, so the idle timeout doesn't hang up.
   Pinned in Task 1 (encoding silence yields a non-empty packet that decodes to 320 samples).
3. **A client sends a packet that decodes but isn't 20 ms** (a 40 ms packet, or a PCM frame
   sent to an Opus session). It must be counted as bad, not pushed as a wrong-sized frame that
   would corrupt ALSA playback. Pinned in Task 1 (40 ms packet and 640-byte PCM each raise) and
   Task 3 (a bad packet is counted and the call continues).
4. **A new page talks to an old server that sends no `codec` in `ready`.** The client must send
   PCM, not Opus. Pinned in Task 4 (`resolveWireCodec(true, undefined) === "pcm"`).
5. **Real audio resumes in the middle of concealment.** The resumed frame must crossfade from
   the last concealed sample, not click. Pinned in Task 2.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `backend/live_intercom/audio/opus.py` | create | ctypes libopus: `load()`, `OpusEncoder`, `OpusDecoder`, `OpusError` |
| `backend/tests/test_opus.py` | create | wrapper tests (skipped without libopus) |
| `backend/live_intercom/audio/jitter.py` | modify | optional `conceal` hook, `concealed_frames` stat |
| `backend/tests/test_jitter.py` | modify | concealment tests; stats dict gains a key |
| `backend/live_intercom/protocol.py` | modify | `CODEC_PCM`, `CODEC_OPUS`, `ready_message(codec)` |
| `backend/tests/test_protocol.py` | modify | ready carries codec |
| `backend/live_intercom/session.py` | modify | negotiate the codec, decode on receive, encode on send, PLC, log |
| `backend/tests/test_session.py` | modify | Opus session tests |
| `backend/live_intercom/web.py` | modify | `opus_available=opus.load()` |
| `deploy/remote-setup.sh` | modify | `libopus0` in the apt list |
| `frontend/package.json`, `package-lock.json` | modify | `@evan/wasm` 0.0.95 |
| `frontend/src/evan-wasm-opus.d.ts` | create | types for the untyped deep import |
| `frontend/src/codec.ts` | create | `createOpusCodec()`, `resolveWireCodec()` |
| `frontend/src/codec.test.ts` | create | WASM round trip and negotiation tests |
| `frontend/src/intercom.ts` | modify | request Opus, follow `ready.codec`, encode/decode |
| `README.md`, `docs/deployment.md` | modify | document the codec |

---

### Task 1: libopus ctypes wrapper

**Files:**
- Create: `backend/live_intercom/audio/opus.py`
- Create: `backend/tests/test_opus.py`
- Modify: `deploy/remote-setup.sh:8`

**Interfaces:**
- Consumes: `protocol.RATE`, `protocol.CHANNELS`, `protocol.FRAME_SAMPLES`,
  `protocol.FRAME_BYTES` (already exist).
- Produces:
  - `load() -> bool`
  - `class OpusError(Exception)`
  - `class OpusEncoder`: `encode(pcm: bytes) -> bytes`, `close() -> None`
  - `class OpusDecoder`: `decode(packet: bytes) -> bytes` (always `FRAME_BYTES`),
    `conceal() -> bytes` (always `FRAME_BYTES`), `close() -> None`
  - Constructing either class when `load()` would return `False` raises `OpusError`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_opus.py`:

```python
import math
import struct

import numpy as np
import pytest

from live_intercom.audio import opus
from live_intercom.protocol import FRAME_BYTES, FRAME_SAMPLES, RATE, SILENCE

pytestmark = pytest.mark.skipif(not opus.load(), reason="libopus not installed (brew install opus)")


def tone(frame_index: int, hz: float = 440.0, amplitude: int = 8000) -> bytes:
    start = frame_index * FRAME_SAMPLES
    return struct.pack(
        f"<{FRAME_SAMPLES}h",
        *(round(amplitude * math.sin(2 * math.pi * hz * (start + i) / RATE)) for i in range(FRAME_SAMPLES)),
    )


def test_round_trip_reproduces_the_tone():
    enc, dec = opus.OpusEncoder(), opus.OpusDecoder()
    sent, received, sizes = [], [], []
    for f in range(50):
        pcm = tone(f)
        packet = enc.encode(pcm)
        sizes.append(len(packet))
        sent.append(pcm)
        received.append(dec.decode(packet))
    assert all(len(frame) == FRAME_BYTES for frame in received)
    assert max(sizes) < 100  # ~20 kbps: roughly 50 bytes per 20 ms, vs 640 for PCM
    a = np.frombuffer(b"".join(sent[10:]), dtype=np.int16).astype(np.float64)
    b = np.frombuffer(b"".join(received[10:]), dtype=np.int16).astype(np.float64)
    # Opus adds a small fixed delay, so compare at the best lag within one frame.
    best = max(np.corrcoef(a[: len(a) - lag], b[lag:])[0, 1] for lag in range(FRAME_SAMPLES))
    assert best > 0.9


def test_silence_still_produces_a_packet_every_frame():
    """No DTX: a muted caller keeps streaming, so the server's idle timeout never fires."""
    enc, dec = opus.OpusEncoder(), opus.OpusDecoder()
    for _ in range(20):
        packet = enc.encode(SILENCE)
        assert len(packet) > 0
        assert len(dec.decode(packet)) == FRAME_BYTES


def test_conceal_returns_one_frame():
    enc, dec = opus.OpusEncoder(), opus.OpusDecoder()
    for f in range(5):
        dec.decode(enc.encode(tone(f)))
    assert len(dec.conceal()) == FRAME_BYTES


@pytest.mark.parametrize(
    "packet",
    [
        b"",  # libopus would silently treat this as a lost packet
        b"\x03",  # code-3 packet with no frame count: libopus rejects it
        bytes(640),  # a PCM frame sent to an Opus session: "decodes" to 160 samples
        bytes([0x50]) + bytes(20),  # a 40 ms SILK packet: too big for a 20 ms frame
    ],
)
def test_invalid_packets_raise(packet):
    with pytest.raises(opus.OpusError):
        opus.OpusDecoder().decode(packet)


def test_encoder_rejects_wrong_frame_size():
    with pytest.raises(ValueError):
        opus.OpusEncoder().encode(bytes(100))


def test_use_after_close_raises_and_close_is_idempotent():
    enc, dec = opus.OpusEncoder(), opus.OpusDecoder()
    enc.close()
    enc.close()
    dec.close()
    dec.close()
    with pytest.raises(opus.OpusError):
        enc.encode(SILENCE)
    with pytest.raises(opus.OpusError):
        dec.decode(b"\x78\x00")
    with pytest.raises(opus.OpusError):
        dec.conceal()  # the ALSA thread may still call this after a hung device stop
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_opus.py -q`
Expected: a collection error, `ImportError: cannot import name 'opus'`.

- [ ] **Step 3: Write the implementation**

Create `backend/live_intercom/audio/opus.py`:

```python
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
        except OSError:
            continue
        _declare(lib)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_opus.py -q`
Expected: `9 passed` (6 tests, one parametrized ×4). If it reports `skipped`, run
`brew install opus` first. The tests must run, not skip.

- [ ] **Step 5: Add libopus0 to the host's apt packages**

In `deploy/remote-setup.sh`, change line 8 from:

```bash
  apt-get install -y python3-venv python3-numpy python3-cffi python3-argon2 libportaudio2
```

to:

```bash
  apt-get install -y python3-venv python3-numpy python3-cffi python3-argon2 libportaudio2 libopus0
```

- [ ] **Step 6: Run the whole backend suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: `176 passed`.

```bash
git add backend/live_intercom/audio/opus.py backend/tests/test_opus.py deploy/remote-setup.sh
git commit -m "feat: add a ctypes libopus wrapper for the WebSocket audio edge"
```

---

### Task 2: Packet-loss concealment in the jitter buffer

**Files:**
- Modify: `backend/live_intercom/audio/jitter.py`
- Modify: `backend/tests/test_jitter.py`

**Interfaces:**
- Consumes: nothing new. `conceal` is any `Callable[[], bytes]` returning `FRAME_BYTES`. In
  Task 3 it's `OpusDecoder.conceal`.
- Produces:
  - `JitterBuffer(max_frames: int, prime_frames: int | None = None, conceal: Callable[[], bytes] | None = None)`
  - `MAX_CONCEAL_FRAMES = 2`
  - `stats()` gains the key `"concealed_frames"`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_jitter.py`, change the import line to:

```python
from live_intercom.audio.jitter import MAX_CONCEAL_FRAMES, JitterBuffer
```

Add this after the `A, B, C, D = ...` line:

```python
P = bytes([9]) * 640  # stands in for decoder-concealed audio (int16 value 0x0909)
```

Change the last assertion of `test_counts_underruns_and_overflow_drops` to:

```python
    assert buf.stats() == {
        "received_frames": 6,
        "gap_frames": 2,
        "underruns": 1,
        "dropped_frames": 2,
        "concealed_frames": 0,
    }
```

Append:

```python
def test_underrun_plays_concealment_then_fades_from_it():
    buf = JitterBuffer(3, conceal=lambda: P)
    buf.push(A)
    buf.push(B)
    buf.pop()
    buf.pop()
    assert [buf.pop() for _ in range(MAX_CONCEAL_FRAMES)] == [P] * MAX_CONCEAL_FRAMES
    gap = samples(buf.pop())
    assert gap[0] == samples(P)[-1]  # the fade continues from the concealed audio, no jump
    assert gap[-1] == 0
    assert buf.pop() == SILENCE
    assert buf.stats()["concealed_frames"] == MAX_CONCEAL_FRAMES


def test_audio_arriving_mid_concealment_crossfades_from_it():
    buf = JitterBuffer(3, prime_frames=1, conceal=lambda: P)
    buf.push(A)
    buf.pop()
    assert buf.pop() == P  # underrun: concealment starts
    buf.push(D)
    resumed = samples(buf.pop())
    assert resumed[0] == samples(P)[-1]  # continuous with the concealed frame
    assert resumed[-1] == samples(D)[-1]  # settles onto the real audio
    assert np.all(np.diff(resumed.astype(np.int64)) <= 0)  # P > D: smooth monotonic ramp down


def test_concealment_run_resets_after_real_audio():
    buf = JitterBuffer(3, prime_frames=1, conceal=lambda: P)
    buf.push(A)
    buf.pop()
    assert buf.pop() == P
    buf.push(A)
    buf.pop()  # real audio again
    assert [buf.pop() for _ in range(MAX_CONCEAL_FRAMES)] == [P] * MAX_CONCEAL_FRAMES


def test_failing_conceal_falls_back_to_fade():
    def boom() -> bytes:
        raise RuntimeError("decoder closed")

    buf = JitterBuffer(3, conceal=boom)
    buf.push(A)
    buf.push(B)
    buf.pop()
    buf.pop()
    gap = samples(buf.pop())
    assert gap[0] == samples(B)[-1]
    assert gap[-1] == 0
    assert buf.stats()["concealed_frames"] == 0


def test_no_concealment_before_any_audio_has_played():
    calls = []
    buf = JitterBuffer(3, conceal=lambda: calls.append(1) or P)
    assert buf.pop() == SILENCE
    buf.push(A)
    assert buf.pop() == SILENCE  # still priming at startup: plain silence
    assert calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_jitter.py -q`
Expected: a collection error, `ImportError: cannot import name 'MAX_CONCEAL_FRAMES'`.

- [ ] **Step 3: Write the implementation**

In `backend/live_intercom/audio/jitter.py`:

Replace the imports block with:

```python
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np

from ..protocol import FRAME_SAMPLES, SILENCE

log = logging.getLogger(__name__)
```

Add this after the `_RAMP = ...` line:

```python
# How long an underrun is bridged with codec concealment before fading to silence: long
# enough to cover a typical late packet, short enough that PLC's synthetic audio (which
# degrades the longer it extrapolates) never becomes noticeable in its own right.
MAX_CONCEAL_FRAMES = 2
```

Append this paragraph to the class docstring, before its closing `"""`:

```python
    When a ``conceal`` callable is given (the Opus decoder's packet-loss concealment), an
    underrun mid-audio first plays up to MAX_CONCEAL_FRAMES frames from it, then fades out
    from the last concealed sample as usual. Real audio resuming during concealment
    crossfades in from the concealed audio.
```

Change `__init__`'s signature and add the new fields at its end:

```python
    def __init__(
        self,
        max_frames: int,
        prime_frames: int | None = None,
        conceal: Callable[[], bytes] | None = None,
    ):
```

```python
        self._conceal = conceal
        self._conceal_run = 0
        self._concealed = 0
```

Replace `pop()` with:

```python
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
```

In `stats()`, add a line after `"dropped_frames": self._dropped,`:

```python
                "concealed_frames": self._concealed,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_jitter.py -q`
Expected: every test passes, including all the existing ones, unchanged apart from the stats
dict.

- [ ] **Step 5: Run the whole backend suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: `181 passed`.

```bash
git add backend/live_intercom/audio/jitter.py backend/tests/test_jitter.py
git commit -m "feat: bridge jitter-buffer underruns with optional codec concealment"
```

---

### Task 3: Negotiate the codec and use Opus in the session

**Files:**
- Modify: `backend/live_intercom/protocol.py`
- Modify: `backend/tests/test_protocol.py`
- Modify: `backend/live_intercom/session.py`
- Modify: `backend/tests/test_session.py`
- Modify: `backend/live_intercom/web.py:80-87`

**Interfaces:**
- Consumes:
  - From Task 1: `opus.load`, `OpusEncoder.encode/close`, `OpusDecoder.decode/conceal/close`,
    `OpusError`.
  - From Task 2: `JitterBuffer(..., conceal=...)`.
- Produces:
  - `protocol.CODEC_PCM = "pcm"`, `protocol.CODEC_OPUS = "opus"`
  - `ready_message(codec: str = CODEC_PCM) -> dict`
  - `SessionManager(..., opus_available: bool = False)`
  - The `/ws` query parameter `codec`
  - The per-call log line gains `codec` and `bad packets`.

- [ ] **Step 1: Write the failing protocol test**

In `backend/tests/test_protocol.py`, replace `test_ready_message` with:

```python
def test_ready_message():
    assert protocol.ready_message() == {
        "type": "ready",
        "rate": 16000,
        "channels": 1,
        "frame_ms": 20,
        "codec": "pcm",
    }
    assert protocol.ready_message(protocol.CODEC_OPUS)["codec"] == "opus"
```

- [ ] **Step 2: Write the failing session tests**

In `backend/tests/test_session.py`, change the imports to:

```python
import asyncio
import logging
import math
import struct
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_intercom.audio import opus
from live_intercom.audio.device import DeviceUnavailable
from live_intercom.protocol import CODEC_OPUS, CODEC_PCM, FRAME_BYTES, SILENCE, ready_message
from live_intercom.session import SessionManager, _close, _send_json
from tests.fakes import FakeDevice, wait_for
```

Change `make_client` to accept and pass through `opus_available`:

```python
def make_client(
    factory,
    idle=5.0,
    pickup_mode="auto",
    ring_timeout=5.0,
    clock=time.monotonic,
    ringtone_file=None,
    opus_available=False,
):
    manager = SessionManager(
        factory,
        jitter_ms=60,
        idle_timeout_s=idle,
        pickup_mode_provider=lambda: pickup_mode,
        ring_timeout_s=ring_timeout,
        clock=clock,
        ringtone_file=ringtone_file,
        opus_available=opus_available,
    )
```

(The rest of `make_client` is unchanged.)

Append:

```python
TONE = struct.pack("<320h", *(round(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(320)))
needs_opus = pytest.mark.skipif(not opus.load(), reason="libopus not installed (brew install opus)")


def test_no_codec_query_gets_pcm():
    with make_client(lambda: FakeDevice(), opus_available=True) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json() == ready_message(CODEC_PCM)


def test_unknown_codec_gets_pcm():
    with make_client(lambda: FakeDevice(), opus_available=True) as client:
        with client.websocket_connect("/ws?codec=flac") as ws:
            assert ws.receive_json() == ready_message(CODEC_PCM)


def test_opus_request_without_libopus_falls_back_to_pcm():
    device = FakeDevice()
    with make_client(lambda: device, opus_available=False) as client:
        with client.websocket_connect("/ws?codec=opus") as ws:
            assert ws.receive_json() == ready_message(CODEC_PCM)
            device.emit_mic(FRAME)
            assert ws.receive_bytes() == FRAME  # raw PCM, untouched


@needs_opus
def test_opus_client_packets_are_decoded_to_the_speaker():
    device = FakeDevice()
    enc = opus.OpusEncoder()
    with make_client(lambda: device, opus_available=True) as client:
        with client.websocket_connect("/ws?codec=opus") as ws:
            assert ws.receive_json() == ready_message(CODEC_OPUS)
            for _ in range(3):
                ws.send_bytes(enc.encode(TONE))

            def heard_audio() -> bool:
                frame = device.pull_speaker()
                assert len(frame) == FRAME_BYTES
                return frame != SILENCE

            assert wait_for(heard_audio)


@needs_opus
def test_opus_host_mic_is_sent_encoded():
    device = FakeDevice()
    dec = opus.OpusDecoder()
    with make_client(lambda: device, opus_available=True) as client:
        with client.websocket_connect("/ws?codec=opus") as ws:
            ws.receive_json()
            device.emit_mic(TONE)
            packet = ws.receive_bytes()
            assert 0 < len(packet) < 200  # an Opus packet, not a 640-byte PCM frame
            assert len(dec.decode(packet)) == FRAME_BYTES


@needs_opus
def test_bad_opus_packet_is_counted_and_the_call_continues(caplog):
    caplog.set_level(logging.INFO, logger="live_intercom.session")
    device = FakeDevice()
    enc = opus.OpusEncoder()
    with make_client(lambda: device, opus_available=True) as client:
        with client.websocket_connect("/ws?codec=opus") as ws:
            ws.receive_json()
            ws.send_bytes(b"\x03")  # invalid packet
            ws.send_bytes(FRAME)  # a PCM frame on an Opus call is also invalid
            for _ in range(3):
                ws.send_bytes(enc.encode(TONE))
            assert wait_for(lambda: device.pull_speaker() != SILENCE)
    assert wait_for(lambda: "codec opus" in caplog.text and "bad packets: 2" in caplog.text)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_protocol.py tests/test_session.py -q`
Expected: a collection error, `ImportError: cannot import name 'CODEC_OPUS'`.

- [ ] **Step 4: Implement the protocol change**

In `backend/live_intercom/protocol.py`, add after `SILENCE = bytes(FRAME_BYTES)`:

```python

# Wire codecs for the binary frames: raw FRAME_BYTES of int16 LE PCM, or one raw Opus packet
# per FRAME_MS frame. Negotiated per call: /ws?codec=opus, answered in ready's "codec".
CODEC_PCM = "pcm"
CODEC_OPUS = "opus"
```

Replace `ready_message`:

```python
def ready_message(codec: str = CODEC_PCM) -> dict:
    return {"type": "ready", "rate": RATE, "channels": CHANNELS, "frame_ms": FRAME_MS, "codec": codec}
```

- [ ] **Step 5: Implement the session change**

In `backend/live_intercom/session.py`:

Replace the local imports:

```python
from .audio.device import AudioDevice, DeviceFactory, DeviceUnavailable
from .audio.jitter import JitterBuffer
from .audio.opus import OpusDecoder, OpusEncoder, OpusError
from .audio.ringtone import RingtoneSource
from .protocol import (
    CODEC_OPUS,
    CODEC_PCM,
    FRAME_BYTES,
    FRAME_MS,
    ready_message,
    rejected_message,
    ringing_message,
)
```

Add the `opus_available: bool = False,` parameter to `SessionManager.__init__` after
`ringtone_file: Path | None = None,`. Add the line `self._opus_available = opus_available`
after `self._ringtone_file = ringtone_file`.

In `_run`, replace:

```python
        jitter = JitterBuffer(self._max_frames)
```

with:

```python
        codec = CODEC_PCM
        encoder: OpusEncoder | None = None
        decoder: OpusDecoder | None = None
        if self._opus_available and ws.query_params.get("codec") == CODEC_OPUS:
            try:
                encoder, decoder = OpusEncoder(), OpusDecoder()
                codec = CODEC_OPUS
            except OpusError:
                log.exception("opus setup failed; this call uses pcm")
                _close_codec(encoder, decoder)
                encoder = decoder = None
        # Opus packets are decoded on arrival (see receive()), so the buffer holds PCM either
        # way; with Opus its underruns are bridged with the decoder's loss concealment.
        jitter = JitterBuffer(self._max_frames, conceal=decoder.conceal if decoder else None)
        bad_packets = [0]
        encode_failed = [False]
```

In the device-error early-return branch, add `_close_codec(encoder, decoder)` so it reads:

```python
        if reason is not None:
            if device is not None:
                await _stop_device(device)
            _close_codec(encoder, decoder)
            await _send_json(ws, {"type": "error", "reason": reason})
            self._loop = None
            return
```

In `receive()`, replace:

```python
                    data = message.get("bytes")
                    if data is not None:
                        if len(data) == FRAME_BYTES:
                            jitter.push(data)
```

with:

```python
                    data = message.get("bytes")
                    if data is not None:
                        frame: bytes | None = None
                        if decoder is not None:
                            try:
                                frame = decoder.decode(data)
                            except OpusError:
                                bad_packets[0] += 1
                        elif len(data) == FRAME_BYTES:
                            frame = data
                        if frame is not None:
                            jitter.push(frame)
```

The existing arrival-statistics lines below it (`now = loop.time()` … `arrivals[1] = now`)
stay inside the `if frame is not None:` block, and the indentation of the `continue` doesn't
change. The block should read:

```python
                        if frame is not None:
                            jitter.push(frame)
                            now = loop.time()
                            if arrivals[1]:
                                gap = now - arrivals[1]
                                arrivals[2] = max(arrivals[2], gap)
                                arrivals[3] += gap > 0.1
                            arrivals[1] = now
                        continue
```

Replace `await _send_json(ws, ready_message())` with:

```python
            await _send_json(ws, ready_message(codec))
```

Replace `send_mic`:

```python
            async def send_mic() -> None:
                while True:
                    frame = await mic_queue.get()
                    if encoder is not None:
                        try:
                            frame = encoder.encode(frame)
                        except OpusError:
                            if not encode_failed[0]:
                                log.exception("opus encode failed; skipping frames")
                                encode_failed[0] = True
                            continue
                    await ws.send_bytes(frame)
```

In the `finally` block, change the per-call log statement to:

```python
                log.info(
                    "call ended after %.1fs (codec %s): jitter buffer %s, longest arrival gap "
                    "%.0f ms, gaps over 100 ms: %d, bad packets: %d",
                    loop.time() - arrivals[0],
                    codec,
                    jitter.stats(),
                    arrivals[2] * 1000,
                    arrivals[3],
                    bad_packets[0],
                )
```

Also in the `finally` block, after `await _stop_device(device)`, add a line that frees the
codec once the ALSA thread has stopped pulling:

```python
            await _stop_device(device)
            _close_codec(encoder, decoder)
```

Add this module-level helper after `_stop_device`:

```python
def _close_codec(*codecs: OpusEncoder | OpusDecoder | None) -> None:
    for codec in codecs:
        if codec is not None:
            codec.close()
```

- [ ] **Step 6: Wire libopus loading into the app**

In `backend/live_intercom/web.py`, add `from .audio import opus` next to the other local
imports. Add `opus_available=opus.load(),` as the last argument of the `SessionManager(...)`
call at lines 80-87:

```python
    manager = SessionManager(
        device_factory,
        config.audio.jitter_ms,
        config.session.idle_timeout_s,
        pickup_mode_provider=get_pickup_mode,
        ring_timeout_s=config.session.ring_timeout_s,
        ringtone_file=config.ringtone_file,
        opus_available=opus.load(),
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: `187 passed`. The six new session tests must not be skipped.

- [ ] **Step 8: Commit**

```bash
git add backend/live_intercom/protocol.py backend/tests/test_protocol.py backend/live_intercom/session.py backend/tests/test_session.py backend/live_intercom/web.py
git commit -m "feat: negotiate Opus per call and code audio at the WebSocket edge"
```

---

### Task 4: Browser Opus codec module

**Files:**
- Modify: `frontend/package.json`, `frontend/package-lock.json` (via npm)
- Create: `frontend/src/evan-wasm-opus.d.ts`
- Create: `frontend/src/codec.ts`
- Create: `frontend/src/codec.test.ts`

**Interfaces:**
- Consumes: `@evan/wasm/target/opus/deno.js` (`Encoder`, `Decoder`).
- Produces:
  - `export type WireCodec = "pcm" | "opus"`
  - `export interface AudioCodec { encode(pcm: Int16Array): Uint8Array; decode(packet: Uint8Array): Int16Array; close(): void }`
  - `export async function createOpusCodec(): Promise<AudioCodec>`
  - `export function resolveWireCodec(requestedOpus: boolean, readyCodec: unknown): WireCodec`

- [ ] **Step 1: Add the dependency**

Run: `cd frontend && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm install --save-exact @evan/wasm@0.0.95`
Expected: `package.json` gains `"@evan/wasm": "0.0.95"` under `dependencies`.

- [ ] **Step 2: Add the type declarations**

Create `frontend/src/evan-wasm-opus.d.ts`:

```ts
// @evan/wasm ships no types. This covers the part of its libopus build that codec.ts uses.
declare module "@evan/wasm/target/opus/deno.js" {
  export class Encoder {
    constructor(options?: {
      channels?: 1 | 2;
      sample_rate?: 8000 | 12000 | 16000 | 24000 | 48000;
      application?: "voip" | "audio" | "restricted_lowdelay";
    });
    bitrate: number;
    complexity: number;
    vbr: boolean;
    dtx: boolean;
    /** Encodes one frame of int16 PCM; returns a copy of the packet. */
    encode(pcm: ArrayBufferView): Uint8Array;
    drop(): void;
  }
  export class Decoder {
    constructor(options?: { channels?: 1 | 2; sample_rate?: 8000 | 12000 | 16000 | 24000 | 48000 });
    /** Decodes one packet; returns a copy of the int16 PCM as bytes. */
    decode(packet: ArrayBufferView): Uint8Array;
    drop(): void;
  }
}
```

- [ ] **Step 3: Write the failing tests**

Create `frontend/src/codec.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { createOpusCodec, resolveWireCodec } from "./codec";

function tone(frame: number): Int16Array {
  const out = new Int16Array(320);
  for (let i = 0; i < 320; i++) {
    out[i] = Math.round(8000 * Math.sin((2 * Math.PI * 440 * (frame * 320 + i)) / 16000));
  }
  return out;
}

describe("createOpusCodec", () => {
  it("round-trips 20 ms frames as small packets", async () => {
    const codec = await createOpusCodec();
    let energy = 0;
    for (let f = 0; f < 25; f++) {
      const packet = codec.encode(tone(f));
      expect(packet.length).toBeGreaterThan(0);
      expect(packet.length).toBeLessThan(100);
      const pcm = codec.decode(packet);
      expect(pcm.length).toBe(320);
      if (f >= 5) for (const s of pcm) energy += s * s;
    }
    expect(Math.sqrt(energy / (20 * 320))).toBeGreaterThan(2000); // the tone survived (RMS ~5657)
    codec.close();
  });

  it("keeps sending packets for silence (no DTX)", async () => {
    const codec = await createOpusCodec();
    for (let f = 0; f < 10; f++) expect(codec.encode(new Int16Array(320)).length).toBeGreaterThan(0);
    codec.close();
  });

  it("rejects packets that are not one 20 ms frame", async () => {
    const codec = await createOpusCodec();
    expect(() => codec.decode(new Uint8Array(0))).toThrow();
    expect(() => codec.decode(new Uint8Array(640))).toThrow(); // PCM sent as Opus: 160 samples
    codec.close();
  });

  it("close is idempotent", async () => {
    const codec = await createOpusCodec();
    codec.close();
    expect(() => codec.close()).not.toThrow();
  });
});

describe("resolveWireCodec", () => {
  it("uses opus only when requested and confirmed", () => {
    expect(resolveWireCodec(true, "opus")).toBe("opus");
    expect(resolveWireCodec(true, "pcm")).toBe("pcm");
    expect(resolveWireCodec(true, undefined)).toBe("pcm"); // older server: no codec field
    expect(resolveWireCodec(false, "opus")).toBe("pcm"); // never decode what we can't
  });
});
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd frontend && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm test`
Expected: FAIL, `Failed to resolve import "./codec"`.

- [ ] **Step 5: Write the implementation**

Create `frontend/src/codec.ts`:

```ts
// Opus at the WebSocket edge (see the backend's audio/opus.py for the host side). The settings
// must match the host: 16 kHz mono, 20 ms frames, VOIP, 20 kbps VBR, complexity 5, no DTX.
const RATE = 16000;
const FRAME_SAMPLES = 320;
const BITRATE = 20000;
const COMPLEXITY = 5;

export type WireCodec = "pcm" | "opus";

export interface AudioCodec {
  /** One 320-sample frame in, one packet out. */
  encode(pcm: Int16Array): Uint8Array;
  /** One packet in, one 320-sample frame out; throws on anything else. */
  decode(packet: Uint8Array): Int16Array;
  close(): void;
}

/** Loads the WASM libopus (a separate chunk, fetched on first call) and creates a codec pair. */
export async function createOpusCodec(): Promise<AudioCodec> {
  const { Encoder, Decoder } = await import("@evan/wasm/target/opus/deno.js");
  const encoder = new Encoder({ channels: 1, sample_rate: RATE, application: "voip" });
  let decoder: InstanceType<typeof Decoder>;
  try {
    encoder.bitrate = BITRATE;
    encoder.complexity = COMPLEXITY;
    encoder.vbr = true;
    // No DTX: the server's idle timeout expects a steady 50 frames/s even when muted.
    encoder.dtx = false;
    decoder = new Decoder({ channels: 1, sample_rate: RATE });
  } catch (err) {
    encoder.drop();
    throw err;
  }
  let closed = false;
  return {
    encode: (pcm) => encoder.encode(pcm),
    decode(packet) {
      // libopus is lenient: empty input would be concealed, other junk often "decodes".
      if (packet.length === 0) throw new Error("empty opus packet");
      const bytes = decoder.decode(packet);
      if (bytes.length !== FRAME_SAMPLES * 2) {
        throw new Error(`opus packet decoded to ${bytes.length / 2} samples, expected ${FRAME_SAMPLES}`);
      }
      return new Int16Array(bytes.buffer, bytes.byteOffset, FRAME_SAMPLES);
    },
    close() {
      if (closed) return;
      closed = true;
      encoder.drop();
      decoder.drop();
    },
  };
}

/** The codec actually in use: Opus only if we asked for it and the server's ready confirmed it. */
export function resolveWireCodec(requestedOpus: boolean, readyCodec: unknown): WireCodec {
  return requestedOpus && readyCodec === "opus" ? "opus" : "pcm";
}
```

- [ ] **Step 6: Run the tests and the type-checked build**

Run: `cd frontend && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm test`
Expected: `13 passed`.

Run: `cd frontend && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm run build`
Expected: success. `codec.ts` isn't imported by the app yet, so there's no Opus chunk until
Task 5.

- [ ] **Step 7: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/evan-wasm-opus.d.ts frontend/src/codec.ts frontend/src/codec.test.ts
git commit -m "feat: add a browser Opus codec module backed by WASM libopus"
```

---

### Task 5: Use Opus in the web client, and document it

**Files:**
- Modify: `frontend/src/intercom.ts`
- Modify: `README.md`
- Modify: `docs/deployment.md`

**Interfaces:**
- Consumes: from Task 4, `createOpusCodec`, `resolveWireCodec`, `AudioCodec`, `WireCodec`.
  From Task 3, the server's `/ws?codec=opus` and `ready.codec`.
- Produces: no new exports. `Intercom`'s public API is unchanged.

There are no DOM-level tests for `intercom.ts`; the negotiation logic it relies on is covered
by `resolveWireCodec`'s tests. This task is verified by the type check, the build and the
manual check in Task 6.

- [ ] **Step 1: Import the codec**

In `frontend/src/intercom.ts`, change the first line to:

```ts
import { type AudioCodec, type WireCodec, createOpusCodec, resolveWireCodec } from "./codec";
import { Framer, StreamResampler, floatToInt16, int16ToFloat } from "./framing";
```

- [ ] **Step 2: Add the codec fields**

Add after `private wakeLock: WakeLockSentinel | null = null;`:

```ts
  /** Loaded before connecting; null if the WASM couldn't load (the call then uses PCM). */
  private codec: AudioCodec | null = null;
  /** What the server's ready message confirmed; decides how binary frames are coded. */
  private wireCodec: WireCodec = "pcm";
```

- [ ] **Step 3: Load the codec before connecting, and decode incoming frames**

In `start()`, replace everything from `const scheme = ...` through the end of the
`ws.onmessage = ...;` assignment with:

```ts
    try {
      this.codec = await createOpusCodec();
    } catch (err) {
      console.warn("opus unavailable, using pcm", err);
    }
    if (this.finished) {
      this.codec?.close();
      this.codec = null;
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const query = this.codec ? "?codec=opus" : "";
    const ws = new WebSocket(`${scheme}://${location.host}/ws${query}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        void this.onControl(JSON.parse(event.data));
      } else if (this.playback && this.upsampler) {
        const pcm = this.decodeFrame(event.data as ArrayBuffer);
        if (!pcm) return;
        const samples = this.upsampler.process(int16ToFloat(pcm));
        this.playback.port.postMessage(samples, [samples.buffer]);
      }
    };
```

Add these two private methods directly after `stop()`:

```ts
  private decodeFrame(data: ArrayBuffer): Int16Array | null {
    if (this.wireCodec !== "opus" || !this.codec) return new Int16Array(data);
    try {
      return this.codec.decode(new Uint8Array(data));
    } catch {
      return null; // skip it: the playback worklet's underrun crossfade covers the gap
    }
  }

  private encodeFrame(frame: Int16Array): Int16Array | Uint8Array | null {
    if (this.wireCodec !== "opus" || !this.codec) return frame;
    try {
      return this.codec.encode(frame);
    } catch {
      return null;
    }
  }
```

- [ ] **Step 4: Follow the server's codec choice**

In `onControl`, change the parameter type and the `ready` branch:

```ts
  private async onControl(message: { type: string; reason?: string; codec?: string }): Promise<void> {
    if (message.type === "ready") {
      this.wireCodec = resolveWireCodec(this.codec !== null, message.codec);
      await this.startAudio();
```

(The remaining branches are unchanged.)

- [ ] **Step 5: Encode outgoing frames**

In `startAudio()`, replace:

```ts
      for (const frame of framer.push(pcm)) {
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      }
```

with:

```ts
      for (const frame of framer.push(pcm)) {
        const payload = this.encodeFrame(frame);
        if (payload && ws.readyState === WebSocket.OPEN) ws.send(payload);
      }
```

- [ ] **Step 6: Free the codec when the call ends**

In `finish()`, replace:

```ts
    this.stream = this.ctx = this.playback = this.upsampler = null;
```

with:

```ts
    this.codec?.close();
    this.codec = null;
    this.wireCodec = "pcm";
    this.stream = this.ctx = this.playback = this.upsampler = null;
```

- [ ] **Step 7: Type check, test and build**

Run: `cd frontend && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm test && PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH npm run build`
Expected: `13 passed`, then a successful build whose output lists a separate JS chunk of about
200 KB for the Opus module. Record its size for the report.

- [ ] **Step 8: Update the docs**

In `README.md`, replace the Features bullet:

```markdown
- Full-duplex voice over one WebSocket: 16 kHz mono 16-bit PCM in 20 ms frames.
```

with:

```markdown
- Full-duplex voice over one WebSocket in 20 ms frames of 16 kHz mono, coded as Opus at about
  20 kbps per direction (WASM libopus in the browser, the system libopus on the host). If
  either side can't load Opus, the call falls back to raw 16-bit PCM (256 kbps), negotiated
  per call.
```

In `README.md`'s "Run it locally" section, add a line after
`Needs Python 3.11 or newer and Node 18 or newer.`:

```markdown
For Opus on the backend, install libopus (`brew install opus` on macOS); without it the
backend runs PCM-only and the Opus tests are skipped.
```

In `docs/deployment.md`, add this to the end of the "Target host" section:

```markdown
Audio travels as Opus (see `backend/live_intercom/audio/opus.py`), using the system
`libopus0` (1.5.2 on Debian 13), which `INSTALL_APT=1` installs. On this board, encoding and
decoding one call costs about 9 % of one core (complexity 5, measured 2026-09-23). If the
library is missing, the service logs `libopus not found; calls will use raw PCM` at startup
and keeps working at about 10× the bandwidth. The per-call log line reports the codec
(`codec opus`), the `bad packets` count and the jitter buffer's `concealed_frames`.
```

- [ ] **Step 9: Commit**

```bash
git add frontend/src/intercom.ts README.md docs/deployment.md
git commit -m "feat: send and receive Opus from the web client, with PCM fallback"
```

---

### Task 6: Deploy and accept on real devices

**Files:** none (verification only). Any fix found here goes into a new commit on the relevant
file.

- [ ] **Step 1: Ask the owner before deploying**

Deploying to `root@192.168.0.17` is outward-facing. Ask: "All tasks are done and the tests
pass. OK to run `deploy/install.sh` (no `INSTALL_APT` needed, libopus0 1.5.2 is already on the
host)?" Wait for a yes.

- [ ] **Step 2: Deploy**

Run: `PATH=~/.nvm/versions/node/v22.18.0/bin:$PATH deploy/install.sh`
Expected: it completes, and the service restarts.

Run: `ssh root@192.168.0.17 'journalctl -u live-intercom -n 30 --no-pager | grep -i opus'`
Expected: no `libopus not found` line.

- [ ] **Step 3: Desktop call over the SSH forward**

- Run `ssh -N -L 8000:127.0.0.1:8000 root@192.168.0.17`, open `http://localhost:8000` in
  Chrome and make a call of about 30 s.
- In DevTools > Network > WS > Messages, binary frames should be about 40–80 bytes, not 640.
- After hanging up, run `ssh root@192.168.0.17 'journalctl -u live-intercom -n 5 --no-pager'`.
  Expected: `call ended ... (codec opus) ... bad packets: 0`.

- [ ] **Step 4: Watch the host's CPU during a call**

During a second call, run `ssh root@192.168.0.17 'top -b -n 3 -d 2 | grep -A0 python'`.
Expected: the service at roughly 10 % CPU or less above its idle figure of 0.5 %.

- [ ] **Step 5: Mobile calls, done by the owner**

Ask the owner to make one call each from:
- iPhone Safari over the Cloudflare tunnel on **cellular**
- Android Chrome over the tunnel

For each call, check the log line for `codec opus`, `bad packets: 0`, and the underrun and
`concealed_frames` counts. Report the numbers. If iPhone shows `codec pcm`, check Safari's
console for `opus unavailable, using pcm` and report the error.

- [ ] **Step 6: Report**

Report to the owner:
- the message sizes seen in step 3;
- the log lines from steps 3 and 5;
- the CPU figure from step 4;
- the Opus chunk size from Task 5.
