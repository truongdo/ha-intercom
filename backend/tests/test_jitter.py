import numpy as np

from live_intercom.audio.jitter import MAX_CONCEAL_FRAMES, JitterBuffer
from live_intercom.protocol import SILENCE

A, B, C, D = (bytes([n]) * 640 for n in (1, 2, 3, 4))
P = bytes([9]) * 640  # stands in for decoder-concealed audio (int16 value 0x0909)


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
    assert buf.stats() == {
        "received_frames": 6,
        "gap_frames": 2,
        "underruns": 1,
        "dropped_frames": 2,
        "concealed_frames": 0,
    }


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
