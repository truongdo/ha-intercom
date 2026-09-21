import numpy as np

from live_intercom.audio.resample import resample, to_channels, to_mono


def test_same_rate_is_identity():
    x = np.arange(320, dtype=np.int16)
    assert np.array_equal(resample(x, 16000, 16000), x)


def test_downsample_48k_to_16k_averages_triplets():
    x = np.array([3, 6, 9, 30, 30, 30], dtype=np.int16)
    out = resample(x, 48000, 16000)
    assert out.dtype == np.int16
    assert out.tolist() == [6, 30]


def test_frame_lengths_for_common_rates():
    for rate in (48000, 44100, 32000, 24000, 8000):
        block = np.zeros(rate * 20 // 1000, dtype=np.int16)
        assert len(resample(block, rate, 16000)) == 320
        assert len(resample(np.zeros(320, dtype=np.int16), 16000, rate)) == len(block)


def test_upsample_interpolates_linearly():
    x = np.array([0, 300], dtype=np.int16)
    out = resample(x, 16000, 48000)
    assert out.tolist() == [0, 100, 200, 300, 300, 300]


def test_to_mono_averages_channels():
    block = np.array([[10, 20], [30, 50]], dtype=np.int16)
    assert to_mono(block).tolist() == [15, 40]


def test_to_channels_duplicates():
    out = to_channels(np.array([1, 2], dtype=np.int16), 2)
    assert out.shape == (2, 2)
    assert out.tolist() == [[1, 1], [2, 2]]


def test_non_integer_downsample_attenuates_aliasing_tone():
    # 15 kHz at 44.1 kHz folds to 1 kHz at 16 kHz unless low-passed first.
    t = np.arange(882)
    tone = (10000 * np.sin(2 * np.pi * 15000 * t / 44100)).astype(np.int16)
    out = resample(tone, 44100, 16000).astype(np.float64)
    assert np.sqrt((out ** 2).mean()) < 0.3 * 10000 / np.sqrt(2)


def test_non_integer_downsample_keeps_dc_and_voice_band():
    assert set(resample(np.full(882, 1000, dtype=np.int16), 44100, 16000).tolist()) == {1000}
    t = np.arange(882)
    tone = (10000 * np.sin(2 * np.pi * 300 * t / 44100)).astype(np.int16)
    out = resample(tone, 44100, 16000).astype(np.float64)
    assert np.sqrt((out ** 2).mean()) > 0.9 * 10000 / np.sqrt(2)
