from live_intercom.audio.ringtone import CYCLE_BYTES, RingtoneSource
from live_intercom.protocol import FRAME_BYTES


def test_frames_are_correct_size():
    source = RingtoneSource()
    for _ in range(500):
        assert len(source.next_frame()) == FRAME_BYTES


def test_cycle_wraps_and_repeats():
    source = RingtoneSource()
    frames_per_cycle = CYCLE_BYTES // FRAME_BYTES
    first_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    second_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    assert first_pass == second_pass


def test_contains_both_tone_and_silence():
    source = RingtoneSource()
    frames = [source.next_frame() for _ in range(CYCLE_BYTES // FRAME_BYTES)]
    silent = bytes(FRAME_BYTES)
    assert any(frame != silent for frame in frames)  # the "on" portion has a tone
    assert any(frame == silent for frame in frames)  # the "off" portion is silent


def test_independent_sources_start_in_sync():
    a, b = RingtoneSource(), RingtoneSource()
    assert a.next_frame() == b.next_frame()
