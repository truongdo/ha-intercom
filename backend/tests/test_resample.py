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
