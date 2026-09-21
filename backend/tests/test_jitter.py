from live_intercom.audio.jitter import JitterBuffer
from live_intercom.protocol import SILENCE

A, B, C, D = (bytes([n]) * 640 for n in (1, 2, 3, 4))


def test_fifo_order():
    buf = JitterBuffer(3)
    for frame in (A, B, C):
        buf.push(frame)
    assert len(buf) == 3
    assert [buf.pop(), buf.pop(), buf.pop()] == [A, B, C]


def test_silence_when_empty():
    assert JitterBuffer(3).pop() == SILENCE


def test_overflow_drops_oldest():
    buf = JitterBuffer(3)
    for frame in (A, B, C, D):
        buf.push(frame)
    assert [buf.pop(), buf.pop(), buf.pop()] == [B, C, D]


def test_withholds_audio_until_primed():
    buf = JitterBuffer(3)  # primes at 2 frames
    buf.push(A)
    assert buf.pop() == SILENCE
    buf.push(B)
    assert [buf.pop(), buf.pop()] == [A, B]


def test_reprimes_after_underrun():
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    assert [buf.pop(), buf.pop()] == [A, B]
    assert buf.pop() == SILENCE  # underrun re-arms priming
    buf.push(C)
    assert buf.pop() == SILENCE
    buf.push(D)
    assert [buf.pop(), buf.pop()] == [C, D]


def test_single_frame_buffer_primes_at_one():
    buf = JitterBuffer(1)
    buf.push(A)
    assert buf.pop() == A


def test_max_depth_drop_oldest_still_holds_when_primed():
    buf = JitterBuffer(3)
    for frame in (A, B, C, D):
        buf.push(frame)
    assert [buf.pop(), buf.pop(), buf.pop()] == [B, C, D]
