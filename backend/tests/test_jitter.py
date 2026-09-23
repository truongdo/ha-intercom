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


def test_overflow_trims_back_to_prime_depth_in_one_go():
    """A burst that overflows the buffer is trimmed once, down to the priming depth, rather
    than one frame per push: each drop is a splice, and a burst over TCP/cellular would
    otherwise splice on every single frame it delivers."""
    buf = JitterBuffer(3)  # primes at 2 frames
    for frame in (A, B, C, D):
        buf.push(frame)
    assert len(buf) == 2
    assert buf.pop() == C  # nothing played yet: no splice to smooth
    assert buf.pop() == D


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


def test_overflow_while_playing_crossfades_across_the_splice():
    """Dropping frames mid-playback jumps from the last played sample into unrelated later
    audio; the next frame must ramp from that last sample instead of cutting to it."""
    buf = JitterBuffer(4, prime_frames=2)
    buf.push(A)
    buf.push(B)
    assert buf.pop() == A  # last played sample is A's (value 0x0101)
    for frame in (D, D, D, D):  # burst: B + 4 frames > 4 -> trimmed back to 2 (D, D)
        buf.push(frame)
    assert len(buf) == 2

    spliced = samples(buf.pop())
    assert spliced[0] == samples(A)[-1]  # continuous with what was just played
    assert spliced[-1] == samples(D)[-1]  # settles onto the new audio
    assert np.all(np.diff(spliced.astype(np.int64)) >= 0)  # smooth, monotonic
    assert buf.pop() == D  # only the first frame after the splice is touched


def test_counts_underruns_and_overflow_drops():
    buf = JitterBuffer(3)
    buf.push(A)
    buf.push(B)
    buf.pop()
    buf.pop()
    buf.pop()  # underrun
    buf.pop()  # still the same underrun, not a second one
    for frame in (A, B, C, D):
        buf.push(frame)
    assert buf.stats() == {"underruns": 1, "dropped_frames": 2}
