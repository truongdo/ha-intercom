import pytest

from live_intercom.audio.alsa import choose_rate, pick_device
from live_intercom.audio.device import DeviceUnavailable

DEVICES = [
    {"name": "HDMI Out", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Jabra SPEAK 710: USB Audio (hw:2,0)", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "Jabra Mic Only", "max_input_channels": 1, "max_output_channels": 0},
]


def test_pick_device_matches_case_insensitively_and_requires_duplex():
    assert pick_device(DEVICES, "jabra") == 1


def test_pick_device_missing_raises():
    with pytest.raises(DeviceUnavailable):
        pick_device(DEVICES, "Yeti")
    with pytest.raises(DeviceUnavailable):
        pick_device(DEVICES[:1], "HDMI")  # output-only is not duplex


def test_choose_rate_prefers_earliest_supported_candidate():
    assert choose_rate(lambda r: r in (48000, 44100)) == 48000
    assert choose_rate(lambda r: True) == 16000


def test_choose_rate_none_supported_raises():
    with pytest.raises(DeviceUnavailable):
        choose_rate(lambda r: False)
