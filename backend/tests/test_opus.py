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
