import wave

from live_intercom.audio.ringtone import CYCLE_BYTES, RingtoneSource
from live_intercom.protocol import FRAME_BYTES, RATE


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


def _write_wav(path, frames: bytes, rate=RATE, channels=1, sampwidth=2) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(rate)
        wav.writeframes(frames)


def test_custom_ringtone_file_is_loaded_and_looped(tmp_path):
    path = tmp_path / "ringtone.wav"
    custom_cycle = bytes(range(256)) * 20  # 5120 bytes = exactly 8 frames, all non-silent
    _write_wav(path, custom_cycle)

    source = RingtoneSource(path)
    frames_per_cycle = len(custom_cycle) // FRAME_BYTES
    first_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    second_pass = [source.next_frame() for _ in range(frames_per_cycle)]
    assert first_pass == second_pass
    assert b"".join(first_pass) == custom_cycle
    # Genuinely different from the default synthesized tone, not silently ignored.
    assert first_pass != [RingtoneSource().next_frame() for _ in range(frames_per_cycle)]


def test_missing_ringtone_file_falls_back_to_default(tmp_path):
    source = RingtoneSource(tmp_path / "does-not-exist.wav")
    default = RingtoneSource()
    assert source.next_frame() == default.next_frame()


def test_wrong_sample_rate_falls_back_to_default(tmp_path):
    path = tmp_path / "ringtone.wav"
    _write_wav(path, bytes(3200), rate=44100)
    source = RingtoneSource(path)
    default = RingtoneSource()
    assert source.next_frame() == default.next_frame()


def test_wrong_channel_count_falls_back_to_default(tmp_path):
    path = tmp_path / "ringtone.wav"
    _write_wav(path, bytes(3200), channels=2)
    source = RingtoneSource(path)
    default = RingtoneSource()
    assert source.next_frame() == default.next_frame()


def test_empty_ringtone_file_falls_back_to_default(tmp_path):
    path = tmp_path / "ringtone.wav"
    _write_wav(path, b"")
    source = RingtoneSource(path)
    default = RingtoneSource()
    assert source.next_frame() == default.next_frame()
