import numpy as np

from live_intercom.audio.jitter import JitterBuffer
from live_intercom.protocol import SILENCE

A, B, C, D = (bytes([n]) * 640 for n in (1, 2, 3, 4))


def samples(frame: bytes) -> np.ndarray:
    return np.frombuffer(frame, dtype=np.int16)


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
    buf.pop()  # underrun re-arms priming; first silent frame fades rather than cuts (see below)
    assert buf.pop() == SILENCE
    buf.push(C)
    assert buf.pop() == SILENCE
    buf.push(D)
    buf.pop()  # first real frame after silence (C) fades in rather than cutting (see below)
    assert buf.pop() == D


def test_underrun_fades_out_instead_of_cutting_to_silence():
    """The first silent frame after real audio ramps down from the last real sample
    instead of jumping straight to zero, so an underrun doesn't sound like a click."""
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    buf.pop()
    buf.pop()  # last real sample played was B's last sample

    gap = samples(buf.pop())
    assert gap[0] == samples(B)[-1]  # continuous with the last real sample, no jump
    assert gap[-1] == 0  # settles to true silence within the frame
    assert np.all(np.diff(gap.astype(np.int64)) <= 0)  # monotonic ramp down, no jitter

    # a second silent frame is plain silence: the fade already happened once
    assert buf.pop() == SILENCE


def test_resume_fades_in_instead_of_cutting_from_silence():
    """The first real frame after an underrun ramps up from zero instead of jumping
    straight to full amplitude, so resuming audio doesn't sound like a click either."""
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    buf.pop()
    buf.pop()
    buf.pop()  # underrun: re-arms priming
    buf.push(C)
    buf.pop()  # still priming
    buf.push(D)

    resumed = samples(buf.pop())
    assert resumed[0] == 0  # continuous with the preceding silence, no jump
    assert resumed[-1] == samples(C)[-1]  # ramps up to the real frame's own content
    assert np.all(np.diff(resumed.astype(np.int64)) >= 0)  # monotonic ramp up

    # the next real frame plays back untouched
    assert buf.pop() == D


def test_single_frame_buffer_primes_at_one():
    buf = JitterBuffer(1)
    buf.push(A)
    assert buf.pop() == A


def test_max_depth_drop_oldest_still_holds_when_primed():
    buf = JitterBuffer(3)
    for frame in (A, B, C, D):
        buf.push(frame)
    assert [buf.pop(), buf.pop(), buf.pop()] == [B, C, D]
