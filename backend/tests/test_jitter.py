from live_intercom.audio.jitter import JitterBuffer
from live_intercom.protocol import SILENCE

A, B, C, D = (bytes([n]) * 640 for n in (1, 2, 3, 4))


def test_fifo_order():
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    assert len(buf) == 2
    assert buf.pop() == A
    assert buf.pop() == B


def test_silence_when_empty():
    assert JitterBuffer(3).pop() == SILENCE


def test_overflow_drops_oldest():
    buf = JitterBuffer(3)
    for frame in (A, B, C, D):
        buf.push(frame)
    assert [buf.pop(), buf.pop(), buf.pop()] == [B, C, D]
